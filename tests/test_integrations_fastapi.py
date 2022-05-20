import uuid
from typing import AsyncGenerator, Dict, List, Optional

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import Depends, FastAPI, status
from fastapi.security.base import SecurityBase
from fastapi.security.http import HTTPBearer
from fastapi.security.oauth2 import OAuth2PasswordBearer
from httpx import Response

from fief_client.client import Fief, FiefAccessTokenInfo, FiefAsync, FiefUserInfo
from fief_client.integrations.fastapi import FiefAuth, FiefClientClass


@pytest.fixture(scope="module", params=[Fief, FiefAsync])
def fief_client(request) -> FiefClientClass:
    fief_class = request.param
    return fief_class("https://bretagne.fief.dev", "CLIENT_ID", "CLIENT_SECRET")


schemes: List[SecurityBase] = [
    HTTPBearer(auto_error=False),
    OAuth2PasswordBearer("/token"),
]


@pytest.fixture(scope="module", params=schemes)
def scheme(request) -> SecurityBase:
    return request.param


@pytest.fixture(scope="module")
def fastapi_app(fief_client: FiefClientClass, scheme: SecurityBase) -> FastAPI:
    class MemoryUserinfoCache:
        def __init__(self) -> None:
            self.storage: Dict[uuid.UUID, FiefUserInfo] = {}

        async def get(self, user_id: uuid.UUID) -> Optional[FiefUserInfo]:
            return self.storage.get(user_id)

        async def set(self, user_id: uuid.UUID, userinfo: FiefUserInfo) -> None:
            self.storage[user_id] = userinfo

    memory_userinfo_cache = MemoryUserinfoCache()

    async def get_memory_userinfo_cache() -> MemoryUserinfoCache:
        return memory_userinfo_cache

    auth = FiefAuth(fief_client, scheme, get_userinfo_cache=get_memory_userinfo_cache)
    app = FastAPI()

    @app.get("/authenticated")
    async def get_authenticated(
        access_token_info: FiefAccessTokenInfo = Depends(auth.authenticated()),
    ):
        return access_token_info

    @app.get("/authenticated-scope")
    async def get_authenticated_scope(
        access_token_info: FiefAccessTokenInfo = Depends(
            auth.authenticated(scope=["required_scope"])
        ),
    ):
        return access_token_info

    @app.get("/current-user")
    async def get_current_user(
        current_user: FiefAccessTokenInfo = Depends(auth.current_user()),
    ):
        return current_user

    @app.get("/current-user-refresh")
    async def get_current_user_refresh(
        current_user: FiefAccessTokenInfo = Depends(auth.current_user(refresh=True)),
    ):
        return current_user

    @app.get("/current-user-scope")
    async def get_current_user_scope(
        current_user: FiefAccessTokenInfo = Depends(
            auth.current_user(scope=["required_scope"])
        ),
    ):
        return current_user

    return app


@pytest_asyncio.fixture
async def test_client(fastapi_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(
        app=fastapi_app, base_url="http://api.bretagne.duchy"
    ) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_openapi(test_client: httpx.AsyncClient, scheme: SecurityBase):
    response = await test_client.get("/openapi.json")

    assert response.status_code == status.HTTP_200_OK

    json = response.json()
    assert scheme.scheme_name in json["components"]["securitySchemes"]


@pytest.mark.asyncio
class TestAuthenticated:
    async def test_missing_token(self, test_client: httpx.AsyncClient):
        response = await test_client.get("/authenticated")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_expired_token(self, test_client: httpx.AsyncClient, generate_token):
        access_token = generate_token(encrypt=False, exp=0)

        response = await test_client.get(
            "/authenticated", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_valid_token(
        self, test_client: httpx.AsyncClient, generate_token, user_id: str
    ):
        access_token = generate_token(encrypt=False, scope="openid")

        response = await test_client.get(
            "/authenticated", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_200_OK

        json = response.json()
        assert json == {
            "id": user_id,
            "scope": ["openid"],
            "access_token": access_token,
        }

    async def test_missing_scope(self, test_client: httpx.AsyncClient, generate_token):
        access_token = generate_token(encrypt=False, scope="openid")

        response = await test_client.get(
            "/authenticated-scope", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_valid_scope(
        self, test_client: httpx.AsyncClient, generate_token, user_id: str
    ):
        access_token = generate_token(encrypt=False, scope="openid required_scope")

        response = await test_client.get(
            "/authenticated-scope", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_200_OK

        json = response.json()
        assert json == {
            "id": user_id,
            "scope": ["openid", "required_scope"],
            "access_token": access_token,
        }


@pytest.mark.asyncio
class TestCurrentUser:
    async def test_missing_token(self, test_client: httpx.AsyncClient):
        response = await test_client.get("/current-user")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_expired_token(self, test_client: httpx.AsyncClient, generate_token):
        access_token = generate_token(encrypt=False, exp=0)

        response = await test_client.get(
            "/current-user", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_valid_token(
        self,
        test_client: httpx.AsyncClient,
        generate_token,
        mock_api_requests: respx.MockRouter,
        user_id: str,
    ):
        mock_api_requests.get("/userinfo").reset()
        mock_api_requests.get("/userinfo").return_value = Response(
            200, json={"sub": user_id}
        )

        access_token = generate_token(encrypt=False, scope="openid")

        response = await test_client.get(
            "/current-user", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_200_OK

        json = response.json()
        assert json == {"sub": user_id}

        # Check cache is working
        response_2 = await test_client.get(
            "/current-user", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response_2.status_code == status.HTTP_200_OK

        json = response_2.json()
        assert json == {"sub": user_id}

        assert mock_api_requests.get("/userinfo").call_count == 1

    async def test_missing_scope(self, test_client: httpx.AsyncClient, generate_token):
        access_token = generate_token(encrypt=False, scope="openid")

        response = await test_client.get(
            "/current-user-scope", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_valid_scope(
        self,
        test_client: httpx.AsyncClient,
        generate_token,
        mock_api_requests: respx.MockRouter,
        user_id: str,
    ):
        mock_api_requests.get("/userinfo").return_value = Response(
            200, json={"sub": user_id}
        )

        access_token = generate_token(encrypt=False, scope="openid required_scope")

        response = await test_client.get(
            "/current-user-scope", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_200_OK

        json = response.json()
        assert json == {"sub": user_id}

    async def test_valid_refresh(
        self,
        test_client: httpx.AsyncClient,
        generate_token,
        mock_api_requests: respx.MockRouter,
        user_id: str,
    ):
        mock_api_requests.get("/userinfo").reset()
        mock_api_requests.get("/userinfo").return_value = Response(
            200, json={"sub": user_id}
        )

        access_token = generate_token(encrypt=False, scope="openid")

        response = await test_client.get(
            "/current-user-refresh", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response.status_code == status.HTTP_200_OK

        json = response.json()
        assert json == {"sub": user_id}

        # Check cache is not used with refresh
        response_2 = await test_client.get(
            "/current-user-refresh", headers={"Authorization": f"Bearer {access_token}"}
        )

        assert response_2.status_code == status.HTTP_200_OK

        json = response_2.json()
        assert json == {"sub": user_id}

        assert mock_api_requests.get("/userinfo").call_count == 2
