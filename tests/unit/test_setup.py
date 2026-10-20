from qexport.cli import CRED_KEYS, parse_credentials, write_env


def test_parse_credentials_from_console_output_with_noise():
    text = ("VM123:1 QISCUS_APP_CODE=abc-123\n'QISCUS_ADMIN_TOKEN=tokA'\nQISCUS_SDK_USER_ID=2017\n"
            "QISCUS_SDK_TOKEN=tokS\nQISCUS_ROLE=admin\nundefined")
    c = parse_credentials(text)
    assert all(c.get(k) for k in CRED_KEYS)
    assert c["QISCUS_ADMIN_TOKEN"] == "tokA" and c["QISCUS_ROLE"] == "admin"


def test_parse_credentials_rejects_the_snippet_itself():
    from qexport.cli import CONSOLE_SNIPPET

    assert parse_credentials(CONSOLE_SNIPPET) == {}


def test_write_env_replaces_credentials_and_keeps_other_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("QISCUS_ADMIN_TOKEN=old\nQISCUS_MESSAGES_RPS=2.0\n", encoding="utf-8")
    write_env(env, {"QISCUS_ADMIN_TOKEN": "new", "QISCUS_APP_CODE": "a"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "QISCUS_ADMIN_TOKEN=new" in lines and "QISCUS_MESSAGES_RPS=2.0" in lines
    assert "QISCUS_ADMIN_TOKEN=old" not in lines
