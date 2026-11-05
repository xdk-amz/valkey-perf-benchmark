"""The single-node server uses the config port and listens on the target address."""

from pathlib import Path
from unittest.mock import patch

from valkey_server import ServerLauncher


def _launcher(tmp_path, config, target_ip="127.0.0.1"):
    launcher = ServerLauncher(
        results_dir=str(tmp_path / "res"),
        valkey_path="/tmp/valkey",
        target_ip=target_ip,
    )
    launcher.config = config
    launcher.modules = []
    return launcher


def _launch_argv(launcher):
    captured = []
    with (
        patch.object(
            launcher, "_run", side_effect=lambda cmd, **k: captured.append(cmd)
        ),
        patch.object(launcher, "_wait_for_server_ready"),
    ):
        launcher._launch_server(tls_mode=False, cluster_mode=False)
    return captured[0]


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_config_port_reaches_the_server(tmp_path):
    argv = _launch_argv(_launcher(tmp_path, {"port": 16479}))
    assert _flag(argv, "--port") == "16479"


def test_default_port_without_config(tmp_path):
    argv = _launch_argv(_launcher(tmp_path, {}))
    assert _flag(argv, "--port") == "6379"


def test_binds_to_the_target_address(tmp_path):
    assert _flag(_launch_argv(_launcher(tmp_path, {})), "--bind") == "127.0.0.1"
    argv = _launch_argv(_launcher(tmp_path, {}, target_ip="10.0.0.5"))
    assert _flag(argv, "--bind") == "10.0.0.5"


def test_bind_ip_overrides_the_target(tmp_path):
    argv = _launch_argv(_launcher(tmp_path, {"bind_ip": "10.0.0.9"}))
    assert _flag(argv, "--bind") == "10.0.0.9"


def test_log_file_is_absolute_for_an_absolute_results_dir(tmp_path):
    argv = _launch_argv(_launcher(tmp_path, {}))
    log = _flag(argv, "--logfile")
    assert log == str(
        (tmp_path / "res").resolve() / "valkey_log_cluster_disabled_tls_disabled.log"
    )


def test_management_client_uses_config_port_and_target(tmp_path):
    launcher = _launcher(tmp_path, {"port": 16479}, target_ip="10.0.0.5")
    with patch("valkey_server.valkey.Valkey") as client:
        with launcher._client_context(tls_mode=False):
            pass
    kwargs = client.call_args.kwargs
    assert kwargs["port"] == 16479
    assert kwargs["host"] == "10.0.0.5"


def test_port_wait_uses_config_port(tmp_path):
    launcher = _launcher(tmp_path, {"port": 16479})
    with patch("valkey_server.subprocess.run") as run:
        run.return_value.stdout = "State Recv-Q\n"
        launcher._wait_for_port_available()
    assert "sport = :16479" in run.call_args.args[0]


def test_cluster_node_without_bind_ip_listens_on_the_target(tmp_path):
    launcher = _launcher(tmp_path, {}, target_ip="10.0.0.5")
    captured = []
    with (
        patch.object(
            launcher, "_run", side_effect=lambda cmd, **k: captured.append(cmd)
        ),
        patch("valkey_server.valkey.Valkey"),
    ):
        launcher._launch_cluster_node(
            port=7000,
            cpu_range=None,
            bind_ip=None,
            tls_mode=False,
            io_threads=None,
            module_path=None,
            node_id=0,
        )
    argv = captured[0]
    assert _flag(argv, "--bind") == "10.0.0.5"
    assert _flag(argv, "--port") == "7000"
