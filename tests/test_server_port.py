"""ServerLauncher uses the configured port for the server and its clients."""

from unittest.mock import MagicMock, patch

from valkey_server import DEFAULT_PORT, ServerLauncher


def _launch(config):
    launcher = ServerLauncher(results_dir="results", valkey_path="/tmp/valkey")
    with (
        patch.object(launcher, "_run") as run,
        patch.object(launcher, "_create_client", return_value=MagicMock()) as client,
        patch.object(launcher, "_reset_ext_storage_file"),
    ):
        launcher.launch(cluster_mode=False, tls_mode=False, config=config)
    argv = run.call_args.args[0]
    return launcher, argv, client


def _port_arg(argv):
    return argv[argv.index("--port") + 1]


def test_configured_port_reaches_server_and_clients():
    launcher, argv, client = _launch({"port": 16379})
    assert _port_arg(argv) == "16379"
    assert launcher.port == 16379
    assert all(c.kwargs.get("port") == 16379 for c in client.call_args_list)


def test_default_port_without_config():
    launcher, argv, _ = _launch(None)
    assert _port_arg(argv) == str(DEFAULT_PORT)
