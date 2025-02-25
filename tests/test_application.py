import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
from typing import Any, Dict, List, Optional, TypeVar
from uuid import UUID, uuid4

import pkg_resources
import pytest
from guardpost.asynchronous.authentication import AuthenticationHandler
from guardpost.authentication import Identity, User
from rodi import Container, inject

from blacksheep import HTTPException, JSONContent, Request, Response, TextContent
from blacksheep.server.application import Application
from blacksheep.server.bindings import (
    ClientInfo,
    FromBytes,
    FromCookie,
    FromFiles,
    FromHeader,
    FromJSON,
    FromQuery,
    FromRoute,
    FromServices,
    FromText,
    RequestUser,
    ServerInfo,
)
from blacksheep.server.di import dependency_injection_middleware
from blacksheep.server.normalization import ensure_response
from blacksheep.server.responses import status_code, text
from blacksheep.testing.helpers import get_example_scope
from blacksheep.testing.messages import MockMessage
from tests.utils.application import FakeApplication
from tests.utils.folder import ensure_folder


class Item:
    def __init__(self, a, b, c):
        self.a = a
        self.b = b
        self.c = c


@dataclass
class Item2:
    a: str
    b: str
    c: str


class Foo:
    def __init__(self, item) -> None:
        self.item = Item(**item)


@pytest.mark.asyncio
async def test_application_supports_dynamic_attributes(app):
    foo = object()

    assert (
        hasattr(app, "foo") is False
    ), "This test makes sense if such attribute is not defined"
    app.foo = foo  # type: ignore
    assert app.foo is foo  # type: ignore


@pytest.mark.asyncio
async def test_application_service_provider_throws_for_missing_value(app):
    assert app._service_provider is None

    with pytest.raises(TypeError):
        app.service_provider


@pytest.mark.asyncio
async def test_application_get_handler(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home(request):
        pass

    @app.router.get("/foo")
    async def foo(request):
        pass

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    assert app.request is not None
    request: Request = app.request

    assert request is not None

    connection = request.headers[b"connection"]
    assert connection == (b"keep-alive",)


@pytest.mark.asyncio
async def test_application_post_multipart_formdata(app, mock_send, mock_receive):
    @app.router.post("/files/upload")
    async def upload_files(request):
        data = await request.multipart()
        assert data is not None

        assert data[0].name == b"text1"
        assert data[0].file_name is None
        assert data[0].content_type is None
        assert data[0].data == b"text default"

        assert data[1].name == b"text2"
        assert data[1].file_name is None
        assert data[1].content_type is None
        assert data[1].data == "aωb".encode("utf8")

        assert data[2].name == b"file1"
        assert data[2].file_name == b"a.txt"
        assert data[2].content_type == b"text/plain"
        assert data[2].data == b"Content of a.txt.\r\n"

        assert data[3].name == b"file2"
        assert data[3].file_name == b"a.html"
        assert data[3].content_type == b"text/html"
        assert data[3].data == b"<!DOCTYPE html><title>Content of a.html.</title>\r\n"

        assert data[4].name == b"file3"
        assert data[4].file_name == b"binary"
        assert data[4].content_type == b"application/octet-stream"
        assert data[4].data == "aωb".encode("utf8")

        files = await request.files()

        assert files[0].name == b"file1"
        assert files[0].file_name == b"a.txt"
        assert files[0].content_type == b"text/plain"
        assert files[0].data == b"Content of a.txt.\r\n"

        assert files[1].name == b"file2"
        assert files[1].file_name == b"a.html"
        assert files[1].content_type == b"text/html"
        assert files[1].data == b"<!DOCTYPE html><title>Content of a.html.</title>\r\n"

        assert files[2].name == b"file3"
        assert files[2].file_name == b"binary"
        assert files[2].content_type == b"application/octet-stream"
        assert files[2].data == "aωb".encode("utf8")

        file_one = await request.files("file1")
        assert file_one[0].name == b"file1"

        return Response(200)

    boundary = b"---------------------0000000000000000000000001"

    content = b"\r\n".join(
        [
            boundary,
            b'Content-Disposition: form-data; name="text1"',
            b"",
            b"text default",
            boundary,
            b'Content-Disposition: form-data; name="text2"',
            b"",
            "aωb".encode("utf8"),
            boundary,
            b'Content-Disposition: form-data; name="file1"; filename="a.txt"',
            b"Content-Type: text/plain",
            b"",
            b"Content of a.txt.",
            b"",
            boundary,
            b'Content-Disposition: form-data; name="file2"; filename="a.html"',
            b"Content-Type: text/html",
            b"",
            b"<!DOCTYPE html><title>Content of a.html.</title>",
            b"",
            boundary,
            b'Content-Disposition: form-data; name="file3"; filename="binary"',
            b"Content-Type: application/octet-stream",
            b"",
            "aωb".encode("utf8"),
            boundary + b"--",
        ]
    )

    await app(
        get_example_scope(
            "POST",
            "/files/upload",
            [
                [b"content-length", str(len(content)).encode()],
                [b"content-type", b"multipart/form-data; boundary=" + boundary],
            ],
        ),
        mock_receive([content]),
        mock_send,
    )

    assert app.response is not None
    response: Response = app.response

    data = await response.text()

    assert response is not None
    assert response.status == 200, data


@pytest.mark.asyncio
async def test_application_post_handler(app, mock_send, mock_receive):
    called_times = 0

    @app.router.post("/api/cat")
    async def create_cat(request):
        nonlocal called_times
        called_times += 1
        assert request is not None

        content = await request.read()
        assert b'{"name":"Celine","kind":"Persian"}' == content

        data = await request.json()
        assert {"name": "Celine", "kind": "Persian"} == data

        return Response(201, [(b"Server", b"Python/3.7")], JSONContent({"id": "123"}))

    content = b'{"name":"Celine","kind":"Persian"}'

    await app(
        get_example_scope(
            "POST",
            "/api/cat",
            [
                (b"content-length", str(len(content)).encode()),
                (b"content-type", b"application/json"),
            ],
        ),
        mock_receive([content]),
        mock_send,
    )

    response = app.response
    assert called_times == 1
    response_data = await response.json()
    assert {"id": "123"} == response_data


@pytest.mark.asyncio
async def test_application_post_handler_invalid_content_type(
    app, mock_send, mock_receive
):
    called_times = 0

    @app.router.post("/api/cat")
    async def create_cat(request):
        nonlocal called_times
        called_times += 1
        assert request is not None

        content = await request.read()
        assert b'{"name":"Celine","kind":"Persian"}' == content

        data = await request.json()
        assert data is None

        return Response(400)

    content = b'{"name":"Celine","kind":"Persian"}'

    await app(
        get_example_scope(
            "POST",
            "/api/cat",
            [
                (b"content-length", str(len(content)).encode()),
                (b"content-type", b"text/plain"),
            ],
        ),
        mock_receive([content]),
        mock_send,
    )

    response: Response = app.response
    assert called_times == 1
    assert response.status == 400


@pytest.mark.asyncio
async def test_application_post_json_handles_missing_body(app, mock_send, mock_receive):
    @app.router.post("/api/cat")
    async def create_cat(request):
        assert request is not None

        content = await request.read()
        assert b"" == content

        text = await request.text()
        assert "" == text

        data = await request.json()
        assert data is None

        return Response(201)

    await app(
        get_example_scope("POST", "/api/cat", []),
        mock_receive([]),
        mock_send,
    )

    response = app.response
    assert response.status == 201


@pytest.mark.asyncio
async def test_application_returns_400_for_invalid_json(app, mock_send, mock_receive):
    @app.router.post("/api/cat")
    async def create_cat(request):
        await request.json()
        ...

    # invalid JSON:
    content = b'"name":"Celine";"kind":"Persian"'

    await app(
        get_example_scope(
            "POST",
            "/api/cat",
            [
                (b"content-length", str(len(content)).encode()),
                (b"content-type", b"application/json"),
            ],
        ),
        mock_receive([content]),
        mock_send,
    )

    response = app.response
    assert response.status == 400
    assert response.content.body == (
        b"Bad Request: Declared Content-Type is application/json but "
        b"the content cannot be parsed as JSON."
    )


@pytest.mark.asyncio
async def test_application_middlewares_one(app, mock_send, mock_receive):
    calls = []

    async def middleware_one(request, handler):
        nonlocal calls
        calls.append(1)
        response = await handler(request)
        calls.append(2)
        return response

    async def middleware_two(request, handler):
        nonlocal calls
        calls.append(3)
        response = await handler(request)
        calls.append(4)
        return response

    @app.router.get("/")
    async def example(request):
        nonlocal calls
        calls.append(5)
        return Response(200, [(b"Server", b"Python/3.7")], JSONContent({"id": "123"}))

    app.middlewares.append(middleware_one)
    app.middlewares.append(middleware_two)
    app.build_services()
    app.configure_middlewares()

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response is not None
    assert response.status == 200
    assert calls == [1, 3, 5, 4, 2]


@pytest.mark.asyncio
async def test_application_middlewares_as_classes(app, mock_send, mock_receive):
    calls = []

    class MiddlewareExample:
        def __init__(self, calls: List[int], seed: int) -> None:
            self.seed = seed
            self.calls = calls

        def get_seed(self) -> int:
            self.seed += 1
            return self.seed

        async def __call__(self, request, handler):
            self.calls.append(self.get_seed())
            response = await handler(request)
            self.calls.append(self.get_seed())
            return response

    @app.route("/")
    async def example(request):
        nonlocal calls
        calls.append(5)
        return Response(200, [(b"Server", b"Python/3.7")], JSONContent({"id": "123"}))

    app.middlewares.append(MiddlewareExample(calls, 0))
    app.middlewares.append(MiddlewareExample(calls, 2))
    app.build_services()
    app.configure_middlewares()

    await app(get_example_scope("GET", "/"), mock_receive([]), mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response is not None
    assert response.status == 200
    assert calls == [1, 3, 5, 4, 2]


@pytest.mark.asyncio
async def test_application_middlewares_are_applied_only_once(
    app, mock_send, mock_receive
):
    """
    This test checks that the same request handled bound to several routes
    is normalized only once with middlewares, and that more calls to
    configure_middlewares don't apply several times the chain of middlewares.
    """
    calls = []

    async def example(request: Request):
        nonlocal calls
        calls.append(2)
        return None

    app.router.add_get("/", example)
    app.router.add_head("/", example)

    async def middleware(request, handler):
        nonlocal calls
        calls.append(1)
        response = await handler(request)
        return response

    app.build_services()
    app.middlewares.append(middleware)

    for method, _ in {("GET", 1), ("GET", 2), ("HEAD", 1), ("HEAD", 2)}:
        app.configure_middlewares()

        await app(get_example_scope(method, "/"), mock_receive([]), mock_send)

        assert app.response is not None
        response: Response = app.response

        assert response is not None
        assert response.status == 204
        assert calls == [1, 2]

        calls.clear()


@pytest.mark.asyncio
async def test_application_middlewares_two(app, mock_send, mock_receive):
    calls = []

    async def middleware_one(request, handler):
        nonlocal calls
        calls.append(1)
        response = await handler(request)
        calls.append(2)
        return response

    async def middleware_two(request, handler):
        nonlocal calls
        calls.append(3)
        response = await handler(request)
        calls.append(4)
        return response

    async def middleware_three(request, handler):
        nonlocal calls
        calls.append(6)
        response = await handler(request)
        calls.append(7)
        return response

    @app.router.get("/")
    async def example(request):
        nonlocal calls
        calls.append(5)
        return Response(200, [(b"Server", b"Python/3.7")], JSONContent({"id": "123"}))

    app.middlewares.append(middleware_one)
    app.middlewares.append(middleware_two)
    app.middlewares.append(middleware_three)
    app.build_services()
    app.configure_middlewares()

    await app(get_example_scope("GET", "/"), mock_receive([]), mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response is not None
    assert response.status == 200
    assert calls == [1, 3, 6, 5, 7, 4, 2]


@pytest.mark.asyncio
async def test_application_middlewares_skip_handler(app, mock_send, mock_receive):
    calls = []

    async def middleware_one(request, handler):
        nonlocal calls
        calls.append(1)
        response = await handler(request)
        calls.append(2)
        return response

    async def middleware_two(request, handler):
        nonlocal calls
        calls.append(3)
        response = await handler(request)
        calls.append(4)
        return response

    async def middleware_three(request, handler):
        nonlocal calls
        calls.append(6)
        return Response(403)

    @app.router.get("/")
    async def example(request):
        nonlocal calls
        calls.append(5)
        return Response(200, [(b"Server", b"Python/3.7")], JSONContent({"id": "123"}))

    app.middlewares.append(middleware_one)
    app.middlewares.append(middleware_two)
    app.middlewares.append(middleware_three)
    app.build_services()
    app.configure_middlewares()

    await app(get_example_scope("GET", "/"), mock_receive([]), mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response is not None
    assert response.status == 403
    assert calls == [1, 3, 6, 4, 2]


@pytest.mark.asyncio
async def test_application_post_multipart_formdata_files_handler(
    app, mock_send, mock_receive
):
    ensure_folder("out")
    ensure_folder("tests/out")

    @app.router.post("/files/upload")
    async def upload_files(request):
        files = await request.files("files[]")

        # NB: in this example; we save files to output folder and verify
        # that their binaries are identical
        for part in files:
            full_path = pkg_resources.resource_filename(
                __name__, "out/" + part.file_name.decode()
            )
            with open(full_path, mode="wb") as saved_file:
                saved_file.write(part.data)

        return Response(200)

    boundary = b"---------------------0000000000000000000000001"
    lines = []

    file_names = {
        "pexels-photo-126407.jpeg",
        "pexels-photo-302280.jpeg",
        "pexels-photo-730896.jpeg",
    }

    rel_path = "files/"

    for file_name in file_names:
        full_path = pkg_resources.resource_filename(__name__, rel_path + file_name)
        with open(full_path, mode="rb") as source_file:
            binary = source_file.read()
            lines += [
                boundary,
                b'Content-Disposition: form-data; name="files[]"; filename="'
                + file_name.encode()
                + b'"',
                b"",
                binary,
            ]

    lines += [boundary + b"--"]
    content = b"\r\n".join(lines)

    await app(
        get_example_scope(
            "POST",
            "/files/upload",
            [
                [b"content-length", str(len(content)).encode()],
                [b"content-type", b"multipart/form-data; boundary=" + boundary],
            ],
        ),
        mock_receive([content]),
        mock_send,
    )

    assert app.response is not None
    response: Response = app.response

    body = await response.text()
    assert response.status == 200, body

    # now files are in both folders: compare to ensure they are identical
    for file_name in file_names:
        full_path = pkg_resources.resource_filename(__name__, rel_path + file_name)
        copy_full_path = pkg_resources.resource_filename(__name__, "./out/" + file_name)

        with open(full_path, mode="rb") as source_file:
            binary = source_file.read()
            with open(copy_full_path, mode="rb") as file_clone:
                clone_binary = file_clone.read()

                assert binary == clone_binary


@pytest.mark.asyncio
async def test_application_http_exception_handlers(app, mock_send, mock_receive):
    called = False

    async def exception_handler(self, request, http_exception):
        nonlocal called
        assert request is not None
        called = True
        return Response(200, content=TextContent("Called"))

    app.exceptions_handlers[519] = exception_handler

    @app.router.get("/")
    async def home(request):
        raise HTTPException(519)

    await app(get_example_scope("GET", "/"), mock_receive, mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response is not None
    assert called is True, "Http exception handler was called"

    text = await response.text()
    assert text == "Called", (
        "The response is the one returned by " "defined http exception handler"
    )


@pytest.mark.asyncio
async def test_application_http_exception_handlers_called_in_application_context(
    app, mock_send, mock_receive
):
    async def exception_handler(self, request, http_exception):
        nonlocal app
        assert self is app
        return Response(200, content=TextContent("Called"))

    app.exceptions_handlers[519] = exception_handler

    @app.router.get("/")
    async def home(request):
        raise HTTPException(519)

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)
    assert app.response is not None
    response: Response = app.response

    assert response is not None
    text = await response.text()
    assert text == "Called", (
        "The response is the one returned by " "defined http exception handler"
    )


@pytest.mark.asyncio
async def test_application_user_defined_exception_handlers(
    app, mock_send, mock_receive
):
    called = False

    class CustomException(Exception):
        pass

    async def exception_handler(self, request, exception: CustomException):
        nonlocal called
        assert request is not None
        called = True
        return Response(200, content=TextContent("Called"))

    app.exceptions_handlers[CustomException] = exception_handler

    @app.router.get("/")
    async def home(request):
        raise CustomException()

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response is not None
    assert called is True, "Http exception handler was called"

    text = await response.text()
    assert text == "Called", (
        "The response is the one returned by " "defined http exception handler"
    )


@pytest.mark.asyncio
async def test_user_defined_exception_handlers_called_in_application_context(
    app, mock_send, mock_receive
):
    class CustomException(Exception):
        pass

    async def exception_handler(
        self: FakeApplication, request: Request, exc: CustomException
    ) -> Response:
        nonlocal app
        assert self is app
        assert isinstance(exc, CustomException)
        return Response(200, content=TextContent("Called"))

    app.exceptions_handlers[CustomException] = exception_handler

    @app.router.get("/")
    async def home(request):
        raise CustomException()

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response is not None
    text = await response.text()
    assert text == "Called", (
        "The response is the one returned by " "defined http exception handler"
    )


@pytest.mark.asyncio
async def test_application_exception_handler_decorator_by_custom_exception(
    app, mock_send, mock_receive
):
    expected_handler_response_text = "Called"

    class CustomException(Exception):
        pass

    @app.exception_handler(CustomException)
    async def exception_handler(
        self: FakeApplication, request: Request, exc: CustomException
    ) -> Response:
        nonlocal app
        assert self is app
        assert isinstance(exc, CustomException)
        return Response(200, content=TextContent("Called"))

    @app.router.get("/")
    async def home(request):
        raise CustomException()

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    assert app.response is not None
    response: Response = app.response

    assert response
    actual_response_text = await response.text()
    assert actual_response_text == expected_handler_response_text


@pytest.mark.asyncio
async def test_application_exception_handler_decorator_by_http_status_code(
    app, mock_send, mock_receive
):
    expected_exception_status_code = 519
    expected_handler_response_text = "Called"

    @app.exception_handler(519)
    async def exception_handler(self, request: Request, exc: HTTPException) -> Response:
        assert isinstance(exc, HTTPException)
        assert exc.status == expected_exception_status_code
        return Response(200, content=TextContent("Called"))

    @app.router.get("/")
    async def home(request):
        raise HTTPException(519)

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    assert app.response
    response: Response = app.response

    assert response

    actual_response_text = await response.text()

    assert actual_response_text == expected_handler_response_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter,expected_value",
    [("a", "a"), ("foo", "foo"), ("Hello%20World!!%3B%3B", "Hello World!!;;")],
)
async def test_handler_route_value_binding_single(
    parameter, expected_value, app, mock_send, mock_receive
):
    called = False

    @app.router.get("/:value")
    async def home(request, value):
        nonlocal called
        called = True
        assert value == expected_value

    app.normalize_handlers()

    await app(get_example_scope("GET", "/" + parameter), mock_receive(), mock_send)

    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter,expected_a,expected_b",
    [
        ("a/b", "a", "b"),
        ("foo/something", "foo", "something"),
        ("Hello%20World!!%3B%3B/another", "Hello World!!;;", "another"),
    ],
)
async def test_handler_route_value_binding_two(
    parameter, expected_a, expected_b, app, mock_send, mock_receive
):
    @app.router.get("/:a/:b")
    async def home(request, a, b):
        assert a == expected_a
        assert b == expected_b

    app.normalize_handlers()
    await app(get_example_scope("GET", "/" + parameter), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter,expected_value", [("12", 12), ("0", 0), ("16549", 16549)]
)
async def test_handler_route_value_binding_single_int(
    parameter, expected_value, app, mock_send, mock_receive
):
    called = False

    @app.router.get("/:value")
    async def home(request, value: int):
        nonlocal called
        called = True
        assert value == expected_value

    app.normalize_handlers()

    await app(get_example_scope("GET", "/" + parameter), mock_receive(), mock_send)

    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["xx", "x"])
async def test_handler_route_value_binding_single_int_invalid(
    parameter, app, mock_send, mock_receive
):
    called = False

    @app.router.get("/:value")
    async def home(request, value: int):
        nonlocal called
        called = True

    app.normalize_handlers()

    await app(get_example_scope("GET", "/" + parameter), mock_receive(), mock_send)

    assert called is False
    assert app.response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["xx", "x"])
async def test_handler_route_value_binding_single_float_invalid(
    parameter, app, mock_send, mock_receive
):
    called = False

    @app.router.get("/:value")
    async def home(request, value: float):
        nonlocal called
        called = True

    app.normalize_handlers()

    await app(get_example_scope("GET", "/" + parameter), mock_receive(), mock_send)

    assert called is False
    assert app.response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter,expected_value", [("12", 12.0), ("0", 0.0), ("16549.55", 16549.55)]
)
async def test_handler_route_value_binding_single_float(
    parameter, expected_value, app, mock_send, mock_receive
):
    called = False

    @app.router.get("/:value")
    async def home(request, value: float):
        nonlocal called
        called = True
        assert value == expected_value

    app.normalize_handlers()

    await app(get_example_scope("GET", "/" + parameter), mock_receive(), mock_send)

    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter,expected_a,expected_b,expected_c",
    [
        ("a/1/12.50", "a", 1, 12.50),
        ("foo/446/500", "foo", 446, 500.0),
        ("Hello%20World!!%3B%3B/60/88.05", "Hello World!!;;", 60, 88.05),
    ],
)
async def test_handler_route_value_binding_mixed_types(
    parameter, expected_a, expected_b, expected_c, app, mock_send, mock_receive
):
    @app.router.get("/:a/:b/:c")
    async def home(request, a: str, b: int, c: float):
        assert a == expected_a
        assert b == expected_b
        assert c == expected_c

    app.normalize_handlers()
    await app(get_example_scope("GET", "/" + parameter), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_value",
    [
        (b"a=a", ["a"]),
        (b"a=foo", ["foo"]),
        (b"a=Hello%20World!!%3B%3B", ["Hello World!!;;"]),
    ],
)
async def test_handler_query_value_binding_single(
    query, expected_value, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(request, a):
        assert a == expected_value

    app.normalize_handlers()

    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)

    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_value", [(b"a=10", 10), (b"b=20", None), (b"", None)]
)
async def test_handler_query_value_binding_optional_int(
    query, expected_value, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(request, a: Optional[int]):
        assert a == expected_value

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_value",
    [
        (b"a=10", 10.0),
        (b"a=12.6", 12.6),
        (b"a=12.6&c=4", 12.6),
        (b"b=20", None),
        (b"", None),
    ],
)
async def test_handler_query_value_binding_optional_float(
    query, expected_value, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(request, a: Optional[float]):
        assert a == expected_value

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_value",
    [
        (b"a=10", [10.0]),
        (b"a=12.6", [12.6]),
        (b"a=12.6&c=4", [12.6]),
        (b"a=12.6&a=4&a=6.6", [12.6, 4.0, 6.6]),
        (b"b=20", None),
        (b"", None),
    ],
)
async def test_handler_query_value_binding_optional_list(
    query, expected_value, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(request, a: Optional[List[float]]):
        assert a == expected_value

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_a,expected_b,expected_c",
    [
        (b"a=a&b=1&c=12.50", "a", 1, 12.50),
        (b"a=foo&b=446&c=500", "foo", 446, 500.0),
        (b"a=Hello%20World!!%3B%3B&b=60&c=88.05", "Hello World!!;;", 60, 88.05),
    ],
)
async def test_handler_query_value_binding_mixed_types(
    query, expected_a, expected_b, expected_c, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(request, a: str, b: int, c: float):
        assert a == expected_a
        assert b == expected_b
        assert c == expected_c

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_value",
    [
        (
            b"a=Hello%20World!!%3B%3B&a=Hello&a=World",
            ["Hello World!!;;", "Hello", "World"],
        ),
    ],
)
async def test_handler_query_value_binding_list(
    query, expected_value, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(request, a):
        assert a == expected_value

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_value",
    [(b"a=2", [2]), (b"a=2&a=44", [2, 44]), (b"a=1&a=5&a=18", [1, 5, 18])],
)
async def test_handler_query_value_binding_list_of_ints(
    query, expected_value, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(request, a: List[int]):
        assert a == expected_value

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_value",
    [
        (b"a=2", [2.0]),
        (b"a=2.5&a=44.12", [2.5, 44.12]),
        (b"a=1&a=5.55556&a=18.656", [1, 5.55556, 18.656]),
    ],
)
async def test_handler_query_value_binding_list_of_floats(
    query, expected_value, app, mock_send, mock_receive
):
    @app.router.get("/")
    async def home(a: List[float]):
        assert a == expected_value

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_sync_method(app, mock_send, mock_receive):
    @app.router.get("/")
    def home(request):
        pass

    app.normalize_handlers()
    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_from_header(app, mock_send, mock_receive):
    @app.router.get("/")
    def home(request, xx: FromHeader[str]):
        assert xx.value == "Hello World"

    app.normalize_handlers()
    await app(
        get_example_scope("GET", "/", [(b"XX", b"Hello World")]),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_from_header_name_compatible(
    app, mock_send, mock_receive
):
    class AcceptLanguageHeader(FromHeader[str]):
        name = "accept-language"

    @inject()
    @app.router.get("/")
    def home(accept_language: AcceptLanguageHeader):
        assert accept_language.value == "en-US,en;q=0.9,it-IT;q=0.8,it;q=0.7"

    app.normalize_handlers()
    await app(
        get_example_scope("GET", "/", []),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_from_query(app, mock_send, mock_receive):
    @app.router.get("/")
    def home(xx: FromQuery[int]):
        assert xx.value == 20

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=b"xx=20"), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_from_query_implicit_default(
    app, mock_send, mock_receive
):
    @app.router.get("/")
    def get_products(
        page: int = 1,
        size: int = 30,
        search: str = "",
    ):
        return text(f"Page: {page}; size: {size}; search: {search}")

    app.normalize_handlers()
    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 1; size: 30; search: "

    await app(get_example_scope("GET", "/", query=b"page=2"), mock_receive(), mock_send)

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 2; size: 30; search: "

    await app(
        get_example_scope("GET", "/", query=b"page=2&size=50"),
        mock_receive(),
        mock_send,
    )

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 2; size: 50; search: "

    await app(
        get_example_scope("GET", "/", query=b"page=2&size=50&search=foo"),
        mock_receive(),
        mock_send,
    )

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 2; size: 50; search: foo"


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_from_query_default(
    app, mock_send, mock_receive
):
    @app.router.get("/")
    def get_products(
        page: FromQuery[int] = FromQuery(1),
        size: FromQuery[int] = FromQuery(30),
        search: FromQuery[str] = FromQuery(""),
    ):
        return text(f"Page: {page.value}; size: {size.value}; search: {search.value}")

    app.normalize_handlers()

    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 1; size: 30; search: "

    await app(get_example_scope("GET", "/", query=b"page=2"), mock_receive(), mock_send)

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 2; size: 30; search: "

    await app(
        get_example_scope("GET", "/", query=b"page=2&size=50"),
        mock_receive(),
        mock_send,
    )

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 2; size: 50; search: "

    await app(
        get_example_scope("GET", "/", query=b"page=2&size=50&search=foo"),
        mock_receive(),
        mock_send,
    )

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == "Page: 2; size: 50; search: foo"


@pytest.mark.asyncio
async def test_handler_normalize_list_sync_method_from_query_default(
    app, mock_send, mock_receive
):
    @app.router.get("/")
    def example(
        a: FromQuery[List[int]] = FromQuery([1, 2, 3]),
        b: FromQuery[List[int]] = FromQuery([4, 5, 6]),
        c: FromQuery[List[str]] = FromQuery(["x"]),
    ):
        return text(f"A: {a.value}; B: {b.value}; C: {c.value}")

    app.normalize_handlers()
    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == f"A: {[1, 2, 3]}; B: {[4, 5, 6]}; C: {['x']}"

    await app(get_example_scope("GET", "/", query=b"a=1349"), mock_receive(), mock_send)

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == f"A: {[1349]}; B: {[4, 5, 6]}; C: {['x']}"

    await app(
        get_example_scope("GET", "/", query=b"a=1349&c=Hello&a=55"),
        mock_receive(),
        mock_send,
    )

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == f"A: {[1349, 55]}; B: {[4, 5, 6]}; C: {['Hello']}"

    await app(
        get_example_scope("GET", "/", query=b"a=1349&c=Hello&a=55&b=10"),
        mock_receive(),
        mock_send,
    )

    response = app.response
    content = await response.text()

    assert response.status == 200
    assert content == f"A: {[1349, 55]}; B: {[10]}; C: {['Hello']}"


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_without_arguments(
    app, mock_send, mock_receive
):
    @app.router.get("/")
    def home():
        return

    app.normalize_handlers()
    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_from_query_optional(
    app, mock_send, mock_receive
):
    @app.router.get("/")
    def home(xx: FromQuery[Optional[int]], yy: FromQuery[Optional[int]]):
        assert xx.value is None
        assert yy.value == 20

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=b"yy=20"), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_optional_binder(app, mock_send, mock_receive):
    @app.router.get("/1")
    def home1(xx: Optional[FromQuery[int]], yy: Optional[FromQuery[int]]):
        assert xx is None
        assert yy.value == 20

    @app.router.get("/2")
    def home2(xx: Optional[FromQuery[int]]):
        assert xx is not None
        assert xx.value == 10

    @app.router.get("/3")
    def home3(xx: Optional[FromQuery[Optional[int]]]):
        assert xx is not None
        assert xx.value == 10

    app.normalize_handlers()
    await app(get_example_scope("GET", "/1", query=b"yy=20"), mock_receive(), mock_send)
    assert app.response.status == 204

    await app(get_example_scope("GET", "/2", query=b"xx=10"), mock_receive(), mock_send)
    assert app.response.status == 204

    await app(get_example_scope("GET", "/3", query=b"xx=10"), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_sync_method_from_query_optional_list(
    app, mock_send, mock_receive
):
    @app.router.get("/")
    def home(xx: FromQuery[Optional[List[int]]], yy: FromQuery[Optional[List[int]]]):
        assert xx.value is None
        assert yy.value == [20, 55, 64]

    app.normalize_handlers()
    await app(
        get_example_scope("GET", "/", query=b"yy=20&yy=55&yy=64"),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_values",
    [
        [b"xx=hello&xx=world&xx=lorem&xx=ipsum", ["hello", "world", "lorem", "ipsum"]],
        [b"xx=1&xx=2", ["1", "2"]],
        [b"xx=1&yy=2", ["1"]],
    ],
)
async def test_handler_normalize_sync_method_from_query_default_type(
    query, expected_values, app, mock_send, mock_receive
):
    @app.router.get("/")
    def home(request, xx: FromQuery):
        assert xx.value == expected_values

    app.normalize_handlers()
    await app(get_example_scope("GET", "/", query=query), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_normalize_method_without_input(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home():
        pass

    app.normalize_handlers()
    await app(get_example_scope("GET", "/"), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,expected_value",
    [["dashboard", "dashboard"], ["hello_world", "hello_world"]],
)
async def test_handler_from_route(value, expected_value, app, mock_send, mock_receive):
    @app.router.get("/:area")
    async def home(request, area: FromRoute[str]):
        assert area.value == expected_value

    app.normalize_handlers()
    await app(get_example_scope("GET", "/" + value), mock_receive(), mock_send)
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value_one,value_two,expected_value_one,expected_value_two",
    [
        ["en", "dashboard", "en", "dashboard"],
        ["it", "hello_world", "it", "hello_world"],
    ],
)
async def test_handler_two_routes_parameters(
    value_one: str,
    value_two: str,
    expected_value_one: str,
    expected_value_two: str,
    app,
    mock_send,
    mock_receive,
):
    @app.router.get("/:culture_code/:area")
    async def home(culture_code: FromRoute[str], area: FromRoute[str]):
        assert culture_code.value == expected_value_one
        assert area.value == expected_value_two

    app.normalize_handlers()
    await app(
        get_example_scope("GET", "/" + value_one + "/" + value_two),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value_one,value_two,expected_value_one,expected_value_two",
    [
        ["en", "dashboard", "en", "dashboard"],
        ["it", "hello_world", "it", "hello_world"],
    ],
)
async def test_handler_two_routes_parameters_implicit(
    value_one: str,
    value_two: str,
    expected_value_one: str,
    expected_value_two: str,
    app,
    mock_send,
    mock_receive,
):
    @app.router.get("/:culture_code/:area")
    async def home(culture_code, area):
        assert culture_code == expected_value_one
        assert area == expected_value_two

    app.normalize_handlers()
    await app(
        get_example_scope("GET", "/" + value_one + "/" + value_two),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(item: FromJSON[Item]):
        assert item is not None
        value = item.value
        assert value.a == "Hello"
        assert value.b == "World"
        assert value.c == 10

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_without_annotation(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(item: FromJSON):
        assert item is not None
        assert isinstance(item.value, dict)
        value = item.value
        assert value == {"a": "Hello", "b": "World", "c": 10}

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_dict(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(item: FromJSON[dict]):
        assert item is not None
        assert isinstance(item.value, dict)
        value = item.value
        assert value == {"a": "Hello", "b": "World", "c": 10}

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_dict_unannotated(
    app, mock_send, mock_receive
):
    @app.router.post("/")
    async def home(item: FromJSON[Dict]):
        assert item is not None
        assert isinstance(item.value, dict)
        value = item.value
        assert value == {"a": "Hello", "b": "World", "c": 10}

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_dict_annotated(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(item: FromJSON[Dict[str, Any]]):
        assert item is not None
        assert isinstance(item.value, dict)
        value = item.value
        assert value == {"a": "Hello", "b": "World", "c": 10}

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.parametrize(
    "value",
    [
        "Lorem ipsum dolor sit amet",
        "Hello, World",
        "Lorem ipsum dolor sit amet\n" * 200,
    ],
)
@pytest.mark.asyncio
async def test_handler_from_text_parameter(value: str, app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(text: FromText):
        assert text.value == value

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [
                [b"content-type", b"text/plain; charset=utf-8"],
                [b"content-length", str(len(value)).encode()],
            ],
        ),
        mock_receive([value.encode("utf8")]),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.parametrize(
    "value",
    [
        b"Lorem ipsum dolor sit amet",
        b"Hello, World",
        b"Lorem ipsum dolor sit amet\n" * 200,
    ],
)
@pytest.mark.asyncio
async def test_handler_from_bytes_parameter(value: bytes, app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(text: FromBytes):
        assert text.value == value

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [
                [b"content-type", b"text/plain; charset=utf-8"],
                [b"content-length", str(len(value)).encode()],
            ],
        ),
        mock_receive([value]),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_files(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(files: FromFiles):
        assert files is not None
        assert files.value is not None
        assert len(files.value) == 4
        file1 = files.value[0]
        file2 = files.value[1]
        file3 = files.value[2]
        file4 = files.value[3]

        assert file1.name == b"file1"
        assert file1.file_name == b"a.txt"
        assert file1.data == b"Content of a.txt.\r\n"

        assert file2.name == b"file2"
        assert file2.file_name == b"a.html"
        assert file2.data == b"<!DOCTYPE html><title>Content of a.html.</title>\r\n"

        assert file3.name == b"file2"
        assert file3.file_name == b"a.html"
        assert file3.data == b"<!DOCTYPE html><title>Content of a.html.</title>\r\n"

        assert file4.name == b"file3"
        assert file4.file_name == b"binary"
        assert file4.data == b"a\xcf\x89b"

    app.normalize_handlers()
    boundary = b"---------------------0000000000000000000000001"

    content = b"\r\n".join(
        [
            boundary,
            b'Content-Disposition: form-data; name="text1"',
            b"",
            b"text default",
            boundary,
            b'Content-Disposition: form-data; name="text2"',
            b"",
            "aωb".encode("utf8"),
            boundary,
            b'Content-Disposition: form-data; name="file1"; filename="a.txt"',
            b"Content-Type: text/plain",
            b"",
            b"Content of a.txt.",
            b"",
            boundary,
            b'Content-Disposition: form-data; name="file2"; filename="a.html"',
            b"Content-Type: text/html",
            b"",
            b"<!DOCTYPE html><title>Content of a.html.</title>",
            b"",
            boundary,
            b'Content-Disposition: form-data; name="file2"; filename="a.html"',
            b"Content-Type: text/html",
            b"",
            b"<!DOCTYPE html><title>Content of a.html.</title>",
            b"",
            boundary,
            b'Content-Disposition: form-data; name="file3"; filename="binary"',
            b"Content-Type: application/octet-stream",
            b"",
            "aωb".encode("utf8"),
            boundary + b"--",
        ]
    )

    await app(
        get_example_scope(
            "POST",
            "/",
            [
                [b"content-length", str(len(content)).encode()],
                [b"content-type", b"multipart/form-data; boundary=" + boundary],
            ],
        ),
        mock_receive([content]),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_files_handles_empty_body(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(files: FromFiles):
        assert files.value == []

    app.normalize_handlers()

    await app(
        get_example_scope(
            "POST",
            "/",
            [],
        ),
        mock_receive([]),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_missing_property(
    app, mock_send, mock_receive
):
    @app.router.post("/")
    async def home(item: FromJSON[Item]):
        ...

    # Note: the following example missing one of the properties
    # required by the constructor
    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"25"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World"}']),
        mock_send,
    )
    assert app.response.status == 400
    assert (
        b"Bad Request: invalid parameter in request payload, caused by type Item "
        + b"or one of its subproperties."
        in app.response.content.body
    )


@pytest.mark.asyncio
async def test_handler_json_response_implicit(app, mock_send, mock_receive):
    @app.router.get("/")
    async def get_item() -> Item2:
        return Item2("Hello", "World", "!")

    # Note: the following example missing one of the properties
    # required by the constructor
    app.normalize_handlers()
    await app(
        get_example_scope(
            "GET",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 200
    data = await app.response.json()
    assert data == Item2("Hello", "World", "!").__dict__


@pytest.mark.asyncio
async def test_handler_json_response_implicit_no_annotation(
    app, mock_send, mock_receive
):
    @app.router.get("/")
    async def get_item():
        return Item2("Hello", "World", "!")

    # Note: the following example missing one of the properties
    # required by the constructor
    app.normalize_handlers()
    await app(
        get_example_scope(
            "GET",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 200
    data = await app.response.json()
    assert data == Item2("Hello", "World", "!").__dict__


@pytest.mark.asyncio
async def test_handler_text_response_implicit(app, mock_send, mock_receive):
    @app.router.get("/")
    async def get_lorem():
        return "Lorem ipsum"

    # Note: the following example missing one of the properties
    # required by the constructor
    app.normalize_handlers()
    await app(
        get_example_scope(
            "GET",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 200
    data = await app.response.text()
    assert data == "Lorem ipsum"


@pytest.mark.asyncio
async def test_handler_from_json_parameter_missing_property_complex_type(
    app, mock_send, mock_receive
):
    @inject()
    @app.router.post("/")
    async def home(item: FromJSON[Foo]):
        ...

    # Note: the following example missing one of the properties
    # required by the constructor
    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"34"]],
        ),
        mock_receive([b'{"item":{"a":"Hello","b":"World"}}']),
        mock_send,
    )
    assert app.response.status == 400
    assert (
        b"Bad Request: invalid parameter in request payload, caused by type Foo "
        + b"or one of its subproperties."
        in app.response.content.body
    )


@pytest.mark.asyncio
async def test_handler_from_json_parameter_missing_property_array(
    app, mock_send, mock_receive
):
    @app.router.post("/")
    async def home(item: FromJSON[List[Item]]):
        ...

    # Note: the following example missing one of the properties
    # required by the constructor
    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"25"]],
        ),
        mock_receive([b'[{"a":"Hello","b":"World"}]']),
        mock_send,
    )
    assert app.response.status == 400
    assert (
        b"Bad Request: invalid parameter in request payload, caused by type Item"
        in app.response.content.body
    )


@pytest.mark.asyncio
async def test_handler_from_json_parameter_handles_request_without_body(
    app, mock_send, mock_receive
):
    @app.router.post("/")
    async def home(item: FromJSON[Item]):
        return Response(200)

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [],
        ),
        mock_receive([]),
        mock_send,
    )
    assert app.response.status == 400
    assert app.response.content.body == b"Bad Request: Expected request content"


@pytest.mark.asyncio
async def test_handler_from_json_list_of_objects(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(item: FromJSON[List[Item]]):
        assert item is not None
        value = item.value

        item_one = value[0]
        item_two = value[1]
        assert item_one.a == "Hello"
        assert item_one.b == "World"
        assert item_one.c == 10

        assert item_two.a == "Lorem"
        assert item_two.b == "ipsum"
        assert item_two.c == 55

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive(
            [
                b'[{"a":"Hello","b":"World","c":10},'
                + b'{"a":"Lorem","b":"ipsum","c":55}]'
            ]
        ),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.parametrize(
    "expected_type,request_body,expected_result",
    [
        [
            List,
            b'["one","two","three"]',
            ["one", "two", "three"],
        ],
        [
            List[bytes],
            b'["bG9yZW0gaXBzdW0=","aGVsbG8gd29ybGQ=","VGhyZWU="]',
            ["lorem ipsum", "hello world", "Three"],
        ],
        [
            List[str],
            b'["one","two","three"]',
            ["one", "two", "three"],
        ],
        [
            List[int],
            b"[20, 10, 0, 200, 12, 64]",
            [20, 10, 0, 200, 12, 64],
        ],
        [
            List[float],
            b"[20.4, 10.23, 0.12, 200.00, 12.12, 64.01]",
            [20.4, 10.23, 0.12, 200.00, 12.12, 64.01],
        ],
        [
            List[bool],
            b"[true, false, true, true, 1, 0]",
            [True, False, True, True, True, False],
        ],
        [
            List[datetime],
            b'["2020-10-24", "2020-10-24T18:46:19.313346", "2019-05-30"]',
            [
                datetime(2020, 10, 24),
                datetime(2020, 10, 24, 18, 46, 19, 313346),
                datetime(2019, 5, 30),
            ],
        ],
        [
            List[date],
            b'["2020-10-24", "2020-10-24", "2019-05-30"]',
            [date(2020, 10, 24), date(2020, 10, 24), date(2019, 5, 30)],
        ],
        [
            List[UUID],
            b'["d1e7745f-2a20-4181-8249-b7fef73592dd",'
            + b'"0bf95cca-3299-4cc0-93d1-ec8e041f5d3e",'
            + b'"d2d52dde-b174-47e0-8a8e-a07d6a559a3a"]',
            [
                UUID("d1e7745f-2a20-4181-8249-b7fef73592dd"),
                UUID("0bf95cca-3299-4cc0-93d1-ec8e041f5d3e"),
                UUID("d2d52dde-b174-47e0-8a8e-a07d6a559a3a"),
            ],
        ],
    ],
)
@pytest.mark.asyncio
async def test_handler_from_json_list_of_primitives(
    expected_type, request_body, expected_result, app, mock_send, mock_receive
):
    @inject()
    @app.router.post("/")
    async def home(item: FromJSON[expected_type]):
        assert item is not None
        value = item.value
        assert value == expected_result

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(request_body)).encode()],
            ],
        ),
        mock_receive([request_body]),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_dataclass(app, mock_send, mock_receive):
    @dataclass
    class Foo:
        foo: str
        ufo: bool

    @inject()
    @app.router.post("/")
    async def home(item: FromJSON[Foo]):
        assert item is not None
        value = item.value
        assert value.foo == "Hello"
        assert value.ufo is True

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"foo":"Hello","ufo":true}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_default(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(item: FromJSON[Item] = FromJSON(Item("One", "Two", 3))):
        assert item is not None
        value = item.value
        assert value.a == "One"
        assert value.b == "Two"
        assert value.c == 3

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_default_override(
    app, mock_send, mock_receive
):
    @app.router.post("/")
    async def home(item: FromJSON[Item] = FromJSON(Item("One", "Two", 3))):
        assert item is not None
        value = item.value
        assert value.a == "Hello"
        assert value.b == "World"
        assert value.c == 10

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_implicit(app, mock_send, mock_receive):
    @app.router.post("/")
    async def home(item: Item):
        assert item is not None
        assert item.a == "Hello"
        assert item.b == "World"
        assert item.c == 10

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_json_parameter_implicit_default(
    app, mock_send, mock_receive
):
    @app.router.post("/")
    async def home(item: Item = Item(1, 2, 3)):
        assert item is not None
        assert item.a == 1
        assert item.b == 2
        assert item.c == 3

    app.normalize_handlers()
    await app(
        get_example_scope(
            "POST",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_wrong_method_json_parameter_gets_null_if_optional(
    app, mock_send, mock_receive
):
    @app.router.get("/")  # <--- NB: wrong http method for posting payloads
    async def home(item: FromJSON[Optional[Item]]):
        assert item.value is None

    app.normalize_handlers()

    await app(
        get_example_scope(
            "GET",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )

    assert app.response.status == 204


@pytest.mark.asyncio
async def test_handler_from_wrong_method_json_parameter_gets_bad_request(
    app, mock_send, mock_receive
):
    @app.router.get("/")  # <--- NB: wrong http method for posting payloads
    async def home(request, item: FromJSON[Item]):
        assert item.value is None

    app.normalize_handlers()

    await app(
        get_example_scope(
            "GET",
            "/",
            [[b"content-type", b"application/json"], [b"content-length", b"32"]],
        ),
        mock_receive([b'{"a":"Hello","b":"World","c":10}']),
        mock_send,
    )

    # 400 because the annotation FromJSON[Item] makes the item REQUIRED;
    assert app.response.status == 400
    content = await app.response.text()
    assert content == "Bad Request: Expected request content"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameter,expected_value",
    [
        [str, "Hello", "Hello"],
        [int, "1349", 1349],
        [float, "13.2", 13.2],
        [bool, "True", True],
        [bool, "1", True],
        [Optional[bool], "1", True],
        [Optional[bool], "", None],
        [bool, "False", False],
        [Optional[bool], "False", False],
        [date, "2020-5-30", date(2020, 5, 30)],
        [date, "2020-1-1", date(2020, 1, 1)],
        [Optional[date], "", None],
        [
            datetime,
            "2020-10-24T18:46:19.313346",
            datetime(2020, 10, 24, 18, 46, 19, 313346),
        ],
        [bool, "0", False],
        [
            UUID,
            "54b2587a-0afc-40ec-a03d-13223d4bb04d",
            UUID("54b2587a-0afc-40ec-a03d-13223d4bb04d"),
        ],
    ],
)
async def test_valid_query_parameter_parse(
    parameter_type, parameter, expected_value, app, mock_send, mock_receive
):
    @inject()
    @app.router.get("/")
    async def home(foo: FromQuery[parameter_type]):
        assert foo.value == expected_value
        return status_code(200)

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/", [], query=f"foo={parameter}".encode()),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameter,expected_value",
    [
        [str, "Hello", "Hello"],
        [int, "1349", 1349],
        [float, "13.2", 13.2],
        [bool, "True", True],
        [bool, "1", True],
        [Optional[bool], "1", True],
        [Optional[bool], "", None],
        [bool, "False", False],
        [Optional[bool], "False", False],
        [date, "2020-5-30", date(2020, 5, 30)],
        [date, "2020-1-1", date(2020, 1, 1)],
        [Optional[date], "", None],
        [
            datetime,
            "2020-10-24T18:46:19.313346",
            datetime(2020, 10, 24, 18, 46, 19, 313346),
        ],
        [bool, "0", False],
        [
            UUID,
            "54b2587a-0afc-40ec-a03d-13223d4bb04d",
            UUID("54b2587a-0afc-40ec-a03d-13223d4bb04d"),
        ],
    ],
)
async def test_valid_cookie_parameter_parse(
    parameter_type, parameter, expected_value, app, mock_send, mock_receive
):
    @inject()
    @app.router.get("/")
    async def home(foo: FromCookie[parameter_type]):
        assert foo.value == expected_value
        return status_code(200)

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/", [(b"cookie", f"foo={parameter}".encode())]),
        mock_receive(),
        mock_send,
    )
    assert app.response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameters,expected_value",
    [
        [List, ["Hello", "World"], ["Hello", "World"]],
        [List[str], ["Hello", "World"], ["Hello", "World"]],
        [List[int], ["1349"], [1349]],
        [List[int], ["1", "2", "3"], [1, 2, 3]],
        [List[float], ["1.12", "2.30", "3.55"], [1.12, 2.30, 3.55]],
        [List[bool], ["1", "0", "0", "1"], [True, False, False, True]],
        [
            List[date],
            ["2020-5-30", "2019-5-30", "2018-1-1"],
            [date(2020, 5, 30), date(2019, 5, 30), date(2018, 1, 1)],
        ],
        [
            List[datetime],
            ["2020-10-24T18:46:19.313346", "2019-10-24T18:46:19.313346"],
            [
                datetime(2020, 10, 24, 18, 46, 19, 313346),
                datetime(2019, 10, 24, 18, 46, 19, 313346),
            ],
        ],
    ],
)
async def test_valid_query_parameter_list_parse(
    parameter_type, parameters, expected_value, app, mock_send, mock_receive
):
    @inject()
    @app.router.get("/")
    async def home(foo: FromQuery[parameter_type]):
        assert foo.value == expected_value
        return status_code(200)

    app.normalize_handlers()

    query = "&".join(f"foo={parameter}" for parameter in parameters)

    await app(
        get_example_scope("GET", "/", [], query=query.encode()),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameter",
    [
        [int, "nope"],
        [float, "nope"],
        [date, "nope"],
        [Optional[date], "nope"],
        [datetime, "nope"],
        [UUID, "nope"],
    ],
)
async def test_invalid_query_parameter_400(
    parameter_type, parameter, app, mock_send, mock_receive
):
    @inject()
    @app.router.get("/")
    async def home(foo: FromQuery[parameter_type]):
        return status_code(200)

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/", [], query=f"foo={parameter}".encode()),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert "Bad Request: Invalid value ['nope'] for parameter `foo`;" in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameter,expected_value",
    [
        [str, "Hello", "Hello"],
        [int, "1349", 1349],
        [float, "13.2", 13.2],
        [bool, "True", True],
        [bool, "1", True],
        [bool, "False", False],
        [date, "2020-5-30", date(2020, 5, 30)],
        [
            datetime,
            "2020-10-24T18:46:19.313346",
            datetime(2020, 10, 24, 18, 46, 19, 313346),
        ],
        [bool, "0", False],
        [
            UUID,
            "54b2587a-0afc-40ec-a03d-13223d4bb04d",
            UUID("54b2587a-0afc-40ec-a03d-13223d4bb04d"),
        ],
    ],
)
async def test_valid_route_parameter_parse(
    parameter_type, parameter, expected_value, app, mock_send, mock_receive
):
    @inject()
    @app.router.get("/:foo")
    async def home(foo: FromRoute[parameter_type]):
        assert foo.value == expected_value
        return status_code(200)

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/" + parameter, []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameter,expected_value",
    [
        [str, "Hello", "Hello"],
        [int, "1349", 1349],
        [float, "13.2", 13.2],
        [bool, "True", True],
        [bool, "1", True],
        [bool, "False", False],
        [date, "2020-5-30", date(2020, 5, 30)],
        [
            datetime,
            "2020-10-24T18:46:19.313346",
            datetime(2020, 10, 24, 18, 46, 19, 313346),
        ],
        [bool, "0", False],
        [
            UUID,
            "54b2587a-0afc-40ec-a03d-13223d4bb04d",
            UUID("54b2587a-0afc-40ec-a03d-13223d4bb04d"),
        ],
    ],
)
async def test_valid_header_parameter_parse(
    parameter_type, parameter, expected_value, app, mock_send, mock_receive
):

    T = TypeVar("T")

    class XFooHeader(FromHeader[T]):
        name = "X-Foo"

    @inject()
    @app.router.get("/")
    async def home(x_foo: XFooHeader[parameter_type]):
        assert x_foo.value == expected_value
        return status_code(200)

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/", [(b"X-Foo", parameter.encode())]),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameter_one,parameter_two",
    [
        [str, "Hello", "World"],
        [int, "1349", "164"],
        [float, "1.2", "13.3"],
        [bytes, b"example", b"example"],
        [bool, True, False],
        [
            UUID,
            "54b2587a-0afc-40ec-a03d-13223d4bb04d",
            "8ffd8e17-1a38-462f-ba71-3d92e52edf1f",
        ],
    ],
)
async def test_valid_query_parameter(
    parameter_type, parameter_one, parameter_two, app, mock_send, mock_receive
):
    @inject()
    @app.router.get("/")
    async def home(foo: FromQuery[parameter_type]):
        assert isinstance(foo.value, parameter_type)
        if isinstance(foo.value, bytes):
            return text(f"Got: {foo.value.decode('utf8')}")
        return text(f"Got: {foo.value}")

    app.normalize_handlers()

    # f strings handle bytes creating string representations:
    if isinstance(parameter_one, bytes):
        parameter_one = parameter_one.decode("utf8")
    if isinstance(parameter_two, bytes):
        parameter_two = parameter_two.decode("utf8")

    await app(
        get_example_scope("GET", "/", [], query=f"foo={parameter_one}".encode()),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {parameter_one}"

    await app(
        get_example_scope(
            "GET", "/", [], query=f"foo={parameter_one}&foo={parameter_two}".encode()
        ),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {parameter_one}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameter_type,parameter_one,parameter_two",
    [
        [str, "Hello", "World"],
        [int, "1349", "164"],
        [float, "1.2", "13.3"],
        [bool, True, False],
        [
            UUID,
            "54b2587a-0afc-40ec-a03d-13223d4bb04d",
            "8ffd8e17-1a38-462f-ba71-3d92e52edf1f",
        ],
    ],
)
async def test_valid_query_parameter_implicit(
    parameter_type, parameter_one, parameter_two, app, mock_send, mock_receive
):
    @inject()
    @app.router.get("/")
    async def home(request, foo: parameter_type):
        assert isinstance(foo, parameter_type)
        return text(f"Got: {foo}")

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/", [], query=f"foo={parameter_one}".encode()),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {parameter_one}"

    await app(
        get_example_scope(
            "GET", "/", [], query=f"foo={parameter_one}&foo={parameter_two}".encode()
        ),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {parameter_one}"


@pytest.mark.asyncio
async def test_valid_query_parameter_list_of_int(app, mock_send, mock_receive):
    expected_values_1 = [1349]
    expected_values_2 = [1349, 164]

    @app.router.get("/")
    async def home(foo: FromQuery[List[int]]):
        return text(f"Got: {foo.value}")

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/", [], query=b"foo=1349"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {expected_values_1}"

    await app(
        get_example_scope("GET", "/", [], query=b"foo=1349&foo=164"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {expected_values_2}"


@pytest.mark.asyncio
async def test_invalid_query_parameter_int(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home(request, foo: FromQuery[int]):
        ...

    app.normalize_handlers()

    await app(
        get_example_scope(
            "GET",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert content == "Bad Request: Missing query parameter `foo`"

    await app(
        get_example_scope("GET", "/", [], query=b"foo=xxx"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert (
        content == "Bad Request: Invalid value ['xxx'] for parameter `foo`; "
        "expected a valid int."
    )

    await app(
        get_example_scope("GET", "/", [], query=b"foo=xxx&foo=yyy"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert (
        content == "Bad Request: Invalid value ['xxx', 'yyy'] for parameter `foo`; "
        "expected a valid int."
    )


@pytest.mark.asyncio
async def test_invalid_query_parameter_float(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home(request, foo: FromQuery[float]):
        ...

    app.normalize_handlers()

    await app(
        get_example_scope(
            "GET",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert content == "Bad Request: Missing query parameter `foo`"

    await app(
        get_example_scope("GET", "/", [], query=b"foo=xxx"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert (
        content == "Bad Request: Invalid value ['xxx'] for parameter `foo`; "
        "expected a valid float."
    )

    await app(
        get_example_scope("GET", "/", [], query=b"foo=xxx&foo=yyy"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert (
        content == "Bad Request: Invalid value ['xxx', 'yyy'] for parameter `foo`; "
        "expected a valid float."
    )


@pytest.mark.asyncio
async def test_invalid_query_parameter_bool(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home(request, foo: FromQuery[bool]):
        ...

    app.normalize_handlers()

    await app(
        get_example_scope(
            "GET",
            "/",
            [],
        ),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert content == "Bad Request: Missing query parameter `foo`"

    await app(
        get_example_scope("GET", "/", [], query=b"foo=xxx"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert (
        content == "Bad Request: Invalid value ['xxx'] for parameter `foo`; "
        "expected a valid bool."
    )

    await app(
        get_example_scope("GET", "/", [], query=b"foo=xxx&foo=yyy"),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert (
        content == "Bad Request: Invalid value ['xxx', 'yyy'] for parameter `foo`; "
        "expected a valid bool."
    )


@pytest.mark.asyncio
async def test_invalid_query_parameter_uuid(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home(request, foo: FromQuery[UUID]):
        return text(f"Got: {foo.value}")

    value_1 = "99cb720c-26f2-43dd-89ea-"

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/", [], query=b"foo=" + str(value_1).encode()),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert content == (
        f"Bad Request: Invalid value ['{value_1}'] for "
        "parameter `foo`; expected a valid UUID."
    )


@pytest.mark.asyncio
async def test_valid_route_parameter_uuid(app, mock_send, mock_receive):
    @app.router.get("/:foo")
    async def home(request, foo: FromRoute[UUID]):
        return text(f"Got: {foo.value}")

    value_1 = uuid4()

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/" + str(value_1), []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {value_1}"


@pytest.mark.asyncio
async def test_valid_route_parameter_uuid_2(app, mock_send, mock_receive):
    @app.router.get("/:a_id/:b_id")
    async def home(request, a_id: FromRoute[UUID], b_id: FromRoute[UUID]):
        return text(f"Got: {a_id.value} and {b_id.value}")

    value_1 = uuid4()
    value_2 = uuid4()

    app.normalize_handlers()

    await app(
        get_example_scope("GET", f"/{value_1}/{value_2}", []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {value_1} and {value_2}"


@pytest.mark.asyncio
async def test_valid_header_parameter_uuid_list(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home(request, x_foo: FromHeader[List[UUID]]):
        return text(f"Got: {x_foo.value}")

    value_1 = uuid4()
    value_2 = uuid4()

    app.normalize_handlers()

    await app(
        get_example_scope(
            "GET",
            "/",
            [(b"x_foo", str(value_1).encode()), (b"x_foo", str(value_2).encode())],
        ),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {[value_1, value_2]}"


@pytest.mark.asyncio
async def test_invalid_route_parameter_uuid(app, mock_send, mock_receive):
    @app.router.get("/:document_id")
    async def home(request, document_id: FromRoute[UUID]):
        return text(f"Got: {document_id.value}")

    value_1 = "abc"

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/" + str(value_1), []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 400
    content = await app.response.text()
    assert content == (
        f"Bad Request: Invalid value ['{value_1}'] for "
        "parameter `document_id`; expected a valid UUID."
    )


@pytest.mark.asyncio
async def test_valid_route_parameter_uuid_implicit(app, mock_send, mock_receive):
    @app.router.get("/:foo")
    async def home(request, foo: UUID):
        return text(f"Got: {foo}")

    value_1 = uuid4()

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/" + str(value_1), []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == f"Got: {value_1}"


@pytest.mark.asyncio
async def test_route_resolution_order(app, mock_send, mock_receive):
    @app.router.get("/:id")
    async def example_a():
        return text("A")

    @app.router.get("/exact")
    async def example_b():
        return text("B")

    @app.router.get("/:foo/:ufo")
    async def example_c():
        return text("C")

    @app.router.get("/:foo/exact")
    async def example_d():
        return text("D")

    app.normalize_handlers()

    await app(
        get_example_scope("GET", "/exact", []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == "B"

    await app(
        get_example_scope("GET", "/aaa/exact", []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == "D"

    await app(
        get_example_scope("GET", "/aaa/exact/", []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == "D"

    await app(
        get_example_scope("GET", "/aaa/bbb", []),
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == "C"


@pytest.mark.asyncio
async def test_client_server_info_bindings(app, mock_send, mock_receive):
    @app.router.get("/")
    async def home(client: ClientInfo, server: ServerInfo):
        return text(f"Client: {client.value}; Server: {server.value}")

    app.normalize_handlers()
    scope = get_example_scope("GET", "/", [])
    await app(
        scope,
        mock_receive(),
        mock_send,
    )

    assert app.response.status == 200
    content = await app.response.text()
    assert content == (
        f"Client: {tuple(scope.get('client', ''))}; "
        f"Server: {tuple(scope.get('server', ''))}"
    )


@pytest.mark.asyncio
async def test_service_bindings(mock_send, mock_receive):
    container = Container()

    @inject()
    class B:
        def __init__(self) -> None:
            self.foo = "foo"

    @inject()
    class A:
        def __init__(self, b: B) -> None:
            self.dep = b

    container.add_exact_scoped(A)
    container.add_exact_scoped(B)

    app = FakeApplication(services=container)

    @inject()
    @app.router.get("/explicit")
    async def explicit(a: FromServices[A]):
        assert isinstance(a.value, A)
        assert isinstance(a.value.dep, B)
        assert a.value.dep.foo == "foo"
        return text("OK")

    @inject()
    @app.router.get("/implicit")
    async def implicit(a: A):
        assert isinstance(a, A)
        assert isinstance(a.dep, B)
        assert a.dep.foo == "foo"
        return text("OK")

    app.build_services()
    app.normalize_handlers()

    for path in {"/explicit", "/implicit"}:
        scope = get_example_scope("GET", path, [])
        await app(
            scope,
            mock_receive(),
            mock_send,
        )

        content = await app.response.text()
        assert content == "OK"
        assert app.response.status == 200


@pytest.mark.asyncio
async def test_di_middleware_enables_scoped_services_in_handle_signature(
    mock_send, mock_receive
):
    container = Container()

    class OperationContext:
        def __init__(self) -> None:
            self.trace_id = uuid4()

    container.add_exact_scoped(OperationContext)

    first_operation: Optional[OperationContext] = None

    app = FakeApplication(services=container)
    app.middlewares.append(dependency_injection_middleware)

    @inject()
    @app.router.get("/")
    async def home(a: OperationContext, b: OperationContext):
        assert a is b
        nonlocal first_operation
        if first_operation is None:
            first_operation = a
        else:
            assert first_operation is not a

        return text("OK")

    await app.start()

    for _ in range(2):
        scope = get_example_scope("GET", "/", [])
        await app(
            scope,
            mock_receive(),
            mock_send,
        )

        content = await app.response.text()
        assert content == "OK"
        assert app.response.status == 200


@pytest.mark.asyncio
async def test_without_di_middleware_no_support_for_scoped_svcs_in_handler_signature(
    mock_send, mock_receive
):
    container = Container()

    class OperationContext:
        def __init__(self) -> None:
            self.trace_id = uuid4()

    container.add_exact_scoped(OperationContext)
    app = FakeApplication(services=container)

    @inject()
    @app.router.get("/")
    async def home(a: OperationContext, b: OperationContext):
        assert a is not b
        return text("OK")

    await app.start()

    for _ in range(2):
        scope = get_example_scope("GET", "/", [])
        await app(
            scope,
            mock_receive(),
            mock_send,
        )

        content = await app.response.text()
        assert content == "OK"
        assert app.response.status == 200


@pytest.mark.asyncio
async def test_service_bindings_default(mock_send, mock_receive):
    # Extremely unlikely, but still supported if the user defines a default service
    container = Container()

    class B:
        def __init__(self) -> None:
            self.foo = "foo"

    @inject()
    class A:
        def __init__(self, b: B) -> None:
            self.dep = b

    app = FakeApplication(services=container)

    @inject()
    @app.router.get("/explicit")
    async def explicit(a: FromServices[A] = FromServices(A(B()))):
        assert isinstance(a.value, A)
        assert isinstance(a.value.dep, B)
        assert a.value.dep.foo == "foo"
        return text("OK")

    @inject()
    @app.router.get("/implicit")
    async def implicit(a: A = A(B())):
        assert isinstance(a, A)
        assert isinstance(a.dep, B)
        assert a.dep.foo == "foo"
        return text("OK")

    app.build_services()
    app.normalize_handlers()

    for path in {"/explicit", "/implicit"}:
        scope = get_example_scope("GET", path, [])
        await app(
            scope,
            mock_receive(),
            mock_send,
        )

        content = await app.response.text()
        assert content == "OK"
        assert app.response.status == 200


@pytest.mark.asyncio
async def test_service_bindings_default_override(mock_send, mock_receive):
    # Extremely unlikely, but still supported if the user defines a default service
    container = Container()

    @inject()
    class B:
        def __init__(self, value: str) -> None:
            self.foo = value

    @inject()
    class A:
        def __init__(self, b: B) -> None:
            self.dep = b

    # Note: the registered service is used instead of the default function argument
    container.add_instance(A(B("ufo")))
    container.add_instance(B("oof"))

    app = FakeApplication(services=container)

    @inject()
    @app.router.get("/explicit")
    async def explicit(a: FromServices[A] = FromServices(A(B("foo")))):
        assert isinstance(a.value, A)
        assert isinstance(a.value.dep, B)
        assert a.value.dep.foo == "ufo"
        return text("OK")

    @inject()
    @app.router.get("/implicit")
    async def implicit(a: A = A(B("foo"))):
        assert isinstance(a, A)
        assert isinstance(a.dep, B)
        assert a.dep.foo == "ufo"
        return text("OK")

    app.build_services()
    app.normalize_handlers()

    for path in {"/explicit", "/implicit"}:
        scope = get_example_scope("GET", path, [])
        await app(
            scope,
            mock_receive(),
            mock_send,
        )

        content = await app.response.text()
        assert content == "OK"
        assert app.response.status == 200


@pytest.mark.asyncio
async def test_user_binding(app, mock_send, mock_receive):
    class MockAuthHandler(AuthenticationHandler):
        async def authenticate(self, context):
            header_value = context.get_first_header(b"Authorization")
            if header_value:
                data = json.loads(urlsafe_b64decode(header_value).decode("utf8"))
                context.identity = Identity(data, "TEST")
            else:
                context.identity = None
            return context.identity

    app.use_authentication().add(MockAuthHandler())

    @app.router.get("/example-1")
    async def example(user: RequestUser):
        assert user.value is not None
        assert user.value.authentication_mode == "TEST"
        return text(f"User name: {user.value.claims['name']}")

    @app.router.get("/example-2")
    async def example_2(user: User):
        assert user is not None
        assert user.authentication_mode == "TEST"
        return text(f"User name: {user.claims['name']}")

    @app.router.get("/example-3")
    async def example_3(user: Identity):
        assert user is not None
        assert user.authentication_mode == "TEST"
        return text(f"User name: {user.claims['name']}")

    await app.start()

    claims = {"id": "001", "name": "Charlie Brown", "role": "user"}

    for path in ["/example-1", "/example-2", "/example-3"]:
        scope = get_example_scope(
            "GET",
            path,
            [(b"Authorization", urlsafe_b64encode(json.dumps(claims).encode("utf8")))],
        )
        await app(
            scope,
            mock_receive(),
            mock_send,
        )

        content = await app.response.text()
        assert app.response.status == 200
        assert content == "User name: Charlie Brown"


@pytest.mark.asyncio
async def test_use_auth_raises_if_app_is_already_started(app):
    class MockAuthHandler(AuthenticationHandler):
        async def authenticate(self, context):
            header_value = context.get_first_header(b"Authorization")
            if header_value:
                data = json.loads(urlsafe_b64decode(header_value).decode("utf8"))
                context.identity = Identity(data, "TEST")
            else:
                context.identity = None
            return context.identity

    await app.start()

    with pytest.raises(RuntimeError):
        app.use_authentication()

    with pytest.raises(RuntimeError):
        app.use_authorization()


@pytest.mark.asyncio
async def test_default_headers(app, mock_send, mock_receive):
    app.default_headers = (("Example", "Foo"),)

    assert app.default_headers == (("Example", "Foo"),)

    @app.route("/")
    async def home():
        return text("Hello World")

    await app.start()

    await app(
        get_example_scope("GET", "/", []),
        mock_receive(),
        mock_send,
    )

    response = app.response
    assert response.status == 200
    assert response.headers.get_first(b"Example") == b"Foo"


@pytest.mark.asyncio
async def test_start_stop_events(app):
    on_start_called = False
    on_after_start_called = False
    on_stop_called = False

    async def before_start(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_called
        on_start_called = True

    async def after_start(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_after_start_called
        on_after_start_called = True

    async def on_stop(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_stop_called
        on_stop_called = True

    app.on_start += before_start
    app.after_start += after_start
    app.on_stop += on_stop

    await app.start()

    assert on_start_called is True
    assert on_after_start_called is True
    assert on_stop_called is False

    await app.stop()

    assert on_start_called is True
    assert on_after_start_called is True
    assert on_stop_called is True


@pytest.mark.asyncio
async def test_start_stop_multiple_events(app):
    on_start_count = 0
    on_stop_count = 0

    async def before_start_1(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    async def before_start_2(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    async def before_start_3(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    async def on_stop_1(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_stop_count
        on_stop_count += 1

    async def on_stop_2(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_stop_count
        on_stop_count += 1

    app.on_start += before_start_1
    app.on_start += before_start_2
    app.on_start += before_start_3
    app.on_stop += on_stop_1
    app.on_stop += on_stop_2

    await app.start()

    assert on_start_count == 3
    assert on_stop_count == 0

    await app.stop()

    assert on_start_count == 3
    assert on_stop_count == 2


@pytest.mark.asyncio
async def test_start_stop_multiple_events_using_decorators(app: Application):
    on_start_count = 0
    on_stop_count = 0

    @app.on_start
    async def before_start_1(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    @app.on_start
    async def before_start_2(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    @app.on_start
    async def before_start_3(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    @app.on_stop
    async def on_stop_1(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_stop_count
        on_stop_count += 1

    @app.on_stop
    async def on_stop_2(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_stop_count
        on_stop_count += 1

    await app.start()

    assert on_start_count == 3
    assert on_stop_count == 0

    await app.stop()

    assert on_start_count == 3
    assert on_stop_count == 2


@pytest.mark.asyncio
async def test_app_events_decorator_args_support(app: Application):
    @app.on_start
    async def before_start_1(application: FakeApplication) -> None:
        ...

    @app.on_start()
    async def before_start_2(application: FakeApplication) -> None:
        ...


@pytest.mark.asyncio
async def test_start_stop_remove_event_handlers(app):
    on_start_count = 0
    on_stop_count = 0

    async def before_start_1(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    async def before_start_2(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    async def on_stop_1(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_stop_count
        on_stop_count += 1

    async def on_stop_2(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_stop_count
        on_stop_count += 1

    app.on_start += before_start_1
    app.on_start += before_start_2
    app.on_stop += on_stop_1
    app.on_stop += on_stop_2

    app.on_start -= before_start_2
    app.on_stop -= on_stop_2

    await app.start()

    assert on_start_count == 1
    assert on_stop_count == 0

    await app.stop()

    assert on_start_count == 1
    assert on_stop_count == 1


@pytest.mark.asyncio
async def test_start_runs_once(app):
    on_start_count = 0

    async def before_start(application: FakeApplication) -> None:
        assert isinstance(application, FakeApplication)
        assert application is app
        nonlocal on_start_count
        on_start_count += 1

    app.on_start += before_start

    await app.start()

    assert on_start_count == 1

    await app.start()
    await app.start()

    assert on_start_count == 1


@pytest.mark.asyncio
async def test_handles_on_start_error_asgi_lifespan(app, mock_send, mock_receive):
    async def before_start(application: FakeApplication) -> None:
        raise RuntimeError("Crash!")

    app.on_start += before_start

    mock_receive = mock_receive(
        [
            MockMessage({"type": "lifespan.startup"}),
            MockMessage({"type": "lifespan.shutdown"}),
        ]
    )

    await app(
        {"type": "lifespan", "message": "lifespan.startup"}, mock_receive, mock_send
    )

    assert mock_send.messages[0] == {"type": "lifespan.startup.failed"}


@pytest.mark.asyncio
async def test_app_with_mounts_handles_on_start_error_asgi_lifespan(
    app: Application, mock_send, mock_receive
):
    async def before_start(application: FakeApplication) -> None:
        raise RuntimeError("Crash!")

    def foo():
        return "foo"

    other_app = Application()
    other_app.router.add_get("/foo", foo)

    app.mount("/foo", other_app)
    app.on_start += before_start

    mock_receive = mock_receive(
        [
            MockMessage({"type": "lifespan.startup"}),
            MockMessage({"type": "lifespan.shutdown"}),
        ]
    )

    await app(
        {"type": "lifespan", "message": "lifespan.startup"}, mock_receive, mock_send
    )

    assert mock_send.messages[0] == {"type": "lifespan.startup.failed"}


def test_register_controller_types_handle_empty_list(app):
    assert app.register_controllers([]) is None


@pytest.mark.asyncio
async def test_response_normalization_wrapped(app, mock_receive, mock_send):
    app.use_cors(
        allow_methods="GET POST DELETE", allow_origins="https://www.neoteroi.dev"
    )

    def headers(additional_headers):
        def decorator(next_handler):
            @wraps(next_handler)
            async def wrapped(*args, **kwargs) -> Response:
                response = ensure_response(await next_handler(*args, **kwargs))

                for (name, value) in additional_headers:
                    response.add_header(name.encode(), value.encode())

                return response

            return wrapped

        return decorator

    @app.router.get("/")
    @headers((("X-Foo", "Foo"),))
    async def home():
        return "Hello, World"

    await app.start()

    await app(
        get_example_scope("GET", "/", []),
        mock_receive(),
        mock_send,
    )

    response = app.response
    assert response.status == 200
    assert response.headers.get_single(b"X-Foo") == b"Foo"
    assert response.content.body == b"Hello, World"


@pytest.mark.asyncio
async def test_response_normalization_with_cors(app, mock_receive, mock_send):
    app.use_cors(
        allow_methods="GET POST DELETE", allow_origins="https://www.neoteroi.dev"
    )

    @app.router.get("/")
    async def home():
        return "Hello, World"

    await app.start()

    await app(
        get_example_scope("GET", "/", []),
        mock_receive(),
        mock_send,
    )

    response = app.response
    assert response.status == 200
    assert response.content.body == b"Hello, World"

    await app(
        get_example_scope("GET", "/", [(b"Origin", b"https://www.neoteroi.dev")]),
        mock_receive(),
        mock_send,
    )

    response = app.response
    assert response.status == 200
    assert response.content.body == b"Hello, World"
