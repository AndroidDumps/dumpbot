"""Tests for GitLabManager existence checks and error handling.

These cover the API-status contract: only a 404 from a GET means "absent"
(and should trigger a create), a 200 means "present", and anything else is an
error that must surface with status/body/request-id rather than being treated
as absence. They also cover re-fetching after a create that lost a race.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dumpyarabot.gitlab_manager import GitLabManager

TOKEN = "test-token"


def _response(status: int, json_data=None, text: str = "", headers=None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json = MagicMock(return_value=json_data if json_data is not None else {})
    response.text = text
    response.headers = headers or {}
    return response


def _client(get_responses=(), post_responses=()):
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(get_responses))
    client.post = AsyncMock(side_effect=list(post_responses))
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=client)
    context_manager.__aexit__ = AsyncMock(return_value=False)
    return context_manager, client


def _manager(tmp_path) -> GitLabManager:
    return GitLabManager(str(tmp_path))


async def test_ensure_subgroup_returns_existing_id(tmp_path):
    context_manager, client = _client(get_responses=[_response(200, {"id": 42})])

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        group_id = await _manager(tmp_path)._ensure_subgroup_exists("oneplus", TOKEN)

    assert group_id == 42
    client.post.assert_not_awaited()
    client.get.assert_awaited_once_with(
        "https://dumps.tadiphone.dev/api/v4/groups/dumps%2Foneplus",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=300.0,
    )


async def test_ensure_subgroup_creates_on_404(tmp_path):
    context_manager, client = _client(
        get_responses=[_response(404, text='{"message":"404 Not Found"}')],
        post_responses=[_response(201, {"id": 7})],
    )

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        group_id = await _manager(tmp_path)._ensure_subgroup_exists("oneplus", TOKEN)

    assert group_id == 7
    client.post.assert_awaited_once()


async def test_ensure_subgroup_raises_on_unexpected_status(tmp_path):
    context_manager, client = _client(
        get_responses=[
            _response(
                500,
                text="internal error",
                headers={"x-request-id": "req-abc-123"},
            )
        ]
    )

    with (
        patch(
            "dumpyarabot.gitlab_manager.gitlab_http_client",
            return_value=context_manager,
        ),
        pytest.raises(Exception) as exc_info,
    ):
        await _manager(tmp_path)._ensure_subgroup_exists("oneplus", TOKEN)

    message = str(exc_info.value)
    assert "500" in message
    assert "internal error" in message
    assert "req-abc-123" in message
    client.post.assert_not_awaited()


async def test_ensure_subgroup_refetches_after_create_race(tmp_path):
    context_manager, _client_instance = _client(
        get_responses=[
            _response(404, text="missing"),
            _response(200, {"id": 9}),
        ],
        post_responses=[_response(409, text="already exists")],
    )

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        group_id = await _manager(tmp_path)._ensure_subgroup_exists("oneplus", TOKEN)

    assert group_id == 9


async def test_ensure_project_returns_existing_id(tmp_path):
    context_manager, client = _client(get_responses=[_response(200, {"id": 11})])

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        project_id = await _manager(tmp_path)._ensure_project_exists(
            64, "device", TOKEN, "oneplus"
        )

    assert project_id == 11
    client.post.assert_not_awaited()


async def test_ensure_project_creates_on_404(tmp_path):
    context_manager, client = _client(
        get_responses=[_response(404, text="missing")],
        post_responses=[_response(201, {"id": 13})],
    )

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        project_id = await _manager(tmp_path)._ensure_project_exists(
            64, "device", TOKEN, "oneplus"
        )

    assert project_id == 13
    client.post.assert_awaited_once()


async def test_ensure_project_raises_on_unexpected_status(tmp_path):
    context_manager, client = _client(
        get_responses=[_response(403, text="forbidden")]
    )

    with (
        patch(
            "dumpyarabot.gitlab_manager.gitlab_http_client",
            return_value=context_manager,
        ),
        pytest.raises(Exception) as exc_info,
    ):
        await _manager(tmp_path)._ensure_project_exists(
            64, "device", TOKEN, "oneplus"
        )

    assert "403" in str(exc_info.value)
    assert "forbidden" in str(exc_info.value)
    client.post.assert_not_awaited()


async def test_ensure_project_refetches_after_create_race(tmp_path):
    context_manager, _client_instance = _client(
        get_responses=[
            _response(404, text="missing"),
            _response(200, {"id": 17}),
        ],
        post_responses=[_response(409, text="already exists")],
    )

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        project_id = await _manager(tmp_path)._ensure_project_exists(
            64, "device", TOKEN, "oneplus"
        )

    assert project_id == 17


async def test_branch_exists_true_on_200(tmp_path):
    context_manager, client = _client(get_responses=[_response(200, {"name": "main"})])

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        exists = await _manager(tmp_path)._branch_exists(1, "feature/x", TOKEN)

    assert exists is True
    client.get.assert_awaited_once_with(
        "https://dumps.tadiphone.dev/api/v4/projects/1/repository/branches/feature%2Fx",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=300.0,
    )


async def test_branch_exists_false_on_404(tmp_path):
    context_manager, _client_instance = _client(
        get_responses=[_response(404, text="404 Branch Not Found")]
    )

    with patch(
        "dumpyarabot.gitlab_manager.gitlab_http_client", return_value=context_manager
    ):
        exists = await _manager(tmp_path)._branch_exists(1, "main", TOKEN)

    assert exists is False


async def test_branch_exists_raises_on_other_status(tmp_path):
    context_manager, _client_instance = _client(
        get_responses=[
            _response(
                401,
                text="unauthorized",
                headers={"x-gitlab-request-id": "gitlab-1"},
            )
        ]
    )

    with (
        patch(
            "dumpyarabot.gitlab_manager.gitlab_http_client",
            return_value=context_manager,
        ),
        pytest.raises(Exception) as exc_info,
    ):
        await _manager(tmp_path)._branch_exists(1, "main", TOKEN)

    message = str(exc_info.value)
    assert "401" in message
    assert "unauthorized" in message
    assert "gitlab-1" in message
