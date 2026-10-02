from unittest.mock import patch

import servidor_local as server


def _clear_status_cache():
    with server._github_status_lock:
        server._github_status_cache.update({"fetched_at": None, "data": None})


def test_dispatch_uses_github_workflow_dispatch():
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return 204, None

    with (
        patch.object(server, "_github_actions_configured", return_value=True),
        patch.object(server, "github_ingesta_status", return_value={"run": None}),
        patch.object(server, "_github_api_request", side_effect=fake_request),
    ):
        result = server.github_dispatch_ingesta()

    assert result["dispatched"] is True
    assert calls == [
        (
            "POST",
            "/repos/allndougls-dotcom/RENDER/actions/workflows/actualizar-datos.yml/dispatches",
            {"ref": "main"},
        )
    ]


def test_dispatch_does_not_duplicate_an_active_run():
    active = {
        "configured": True,
        "run": {"id": 123, "status": "in_progress"},
    }
    with (
        patch.object(server, "_github_actions_configured", return_value=True),
        patch.object(server, "github_ingesta_status", return_value=active),
        patch.object(server, "_github_api_request") as request,
    ):
        result = server.github_dispatch_ingesta()

    request.assert_not_called()
    assert result["alreadyRunning"] is True
    assert result["run"]["id"] == 123


def test_status_uses_real_github_job_steps_for_progress():
    run = {
        "id": 321,
        "run_number": 64,
        "event": "workflow_dispatch",
        "status": "in_progress",
        "conclusion": None,
        "created_at": "2026-10-02T17:00:00Z",
        "updated_at": "2026-10-02T17:05:00Z",
        "html_url": "https://github.com/allndougls-dotcom/RENDER/actions/runs/321",
        "head_sha": "abcdef1234567890",
    }
    responses = [
        (200, {"workflow_runs": [run]}),
        (200, {"jobs": [{"steps": [
            {"name": "Descargar repositorio", "status": "completed"},
            {"name": "Ejecutar ingesta completa", "status": "in_progress"},
            {"name": "Publicar datos", "status": "pending"},
            {"name": "Post Configurar Python", "status": "pending"},
        ]}]}),
    ]
    _clear_status_cache()
    with (
        patch.object(server, "_github_actions_configured", return_value=True),
        patch.object(server, "_github_api_request", side_effect=responses),
    ):
        status = server.github_ingesta_status(force=True)

    assert status["run"]["runNumber"] == 64
    assert status["progress"] == 33
    assert status["currentStep"] == "Ejecutar ingesta completa"
