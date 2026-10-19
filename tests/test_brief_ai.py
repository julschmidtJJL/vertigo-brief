"""brief_ai.py — one prompt, either provider, the same output contract.

The AI half can run on an OpenAI key or an Anthropic key. Which one is picked
must be predictable (OpenAI first, so a setup with only an OpenAI key behaves
as before), and both must hand back the same thing: the model's text on
stdout, usage on stderr, exit 1 and nothing on stdout when there is no brief.
The `AI`/`ƒ` provenance and the no-generated-numbers rule are in the prompt
and the runner, so the test that matters here is that both providers receive
the prompt byte for byte.

Both SDKs are replaced by stand-ins in sys.modules. No test reaches a network.
"""
import os
import sys
import types

import pytest

import brief_ai

PROMPT = "Theses and news. State no number Python did not hand you.\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BRIEF_AI_PROVIDER",
              "BRIEF_MODEL", "BRIEF_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(brief_ai, "CONFIG_DIR", str(cfg))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(PROMPT)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["brief_ai.py", str(prompt), *args])
        return brief_ai.main()
    run.cfg = cfg
    return run


def key_file(cfg, name, line, mode=0o600):
    p = cfg / name
    p.write_text(line + "\n")
    p.chmod(mode)


# --- stand-in SDKs ------------------------------------------------------------

@pytest.fixture
def fake_openai(monkeypatch):
    calls = []

    class Responses:
        def create(self, **kw):
            calls.append(kw)
            return types.SimpleNamespace(
                output_text="  OpenAI brief.  ",
                usage=types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                            total_tokens=15))

    class OpenAI:
        def __init__(self, **kw):
            calls.append({"client": kw})
            self.responses = Responses()

    monkeypatch.setitem(sys.modules, "openai",
                        types.SimpleNamespace(OpenAI=OpenAI))
    return calls


def block(type_, text=""):
    return types.SimpleNamespace(type=type_, text=text, thinking="")


@pytest.fixture
def fake_anthropic(monkeypatch):
    state = {"calls": [], "raise": None,
             "message": types.SimpleNamespace(
                 content=[block("thinking"), block("text", "Claude brief."),
                          block("text", "Second block.")],
                 stop_reason="end_turn", stop_details=None,
                 usage=types.SimpleNamespace(input_tokens=20, output_tokens=7))}

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return state["message"]

    class Messages:
        def stream(self, **kw):
            state["calls"].append(kw)
            if state["raise"]:
                raise state["raise"]
            return Stream()

    class Anthropic:
        def __init__(self, **kw):
            state["client"] = kw
            self.beta = types.SimpleNamespace(messages=Messages())

    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=Anthropic))
    return state


# --- which provider -----------------------------------------------------------

def test_openai_is_the_default_when_both_keys_are_set(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    assert brief_ai.pick_provider() == ("openai", "sk-o", None)


def test_anthropic_key_alone_selects_anthropic(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    assert brief_ai.pick_provider() == ("anthropic", "sk-a", None)


def test_anthropic_key_file_alone_selects_anthropic(env):
    key_file(env.cfg, "anthropic.env", 'ANTHROPIC_API_KEY="sk-file"')
    assert brief_ai.pick_provider() == ("anthropic", "sk-file", None)


def test_explicit_provider_wins_over_detection(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    assert brief_ai.pick_provider("Anthropic") == ("anthropic", "sk-a", None)


def test_explicit_provider_without_its_key_fails_instead_of_switching(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    provider, key, err = brief_ai.pick_provider("anthropic")
    assert key is None and "ANTHROPIC_API_KEY" in err


def test_empty_provider_counts_as_unset(env, monkeypatch):
    # An unset Actions variable arrives as "", not as a missing variable.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert brief_ai.pick_provider("") == ("anthropic", "sk-a", None)


def test_unknown_provider_is_refused(env):
    _, _, err = brief_ai.pick_provider("gemini")
    assert "unknown provider" in err


def test_no_key_names_both_options(env, capsys):
    assert env() == 1
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err and "ANTHROPIC_API_KEY" in err


def test_world_readable_key_file_is_refused(env):
    key_file(env.cfg, "anthropic.env", "ANTHROPIC_API_KEY=sk-a", mode=0o644)
    _, key, err = brief_ai.pick_provider()
    assert key is None and "chmod 600" in err


# --- the OpenAI path ----------------------------------------------------------

def test_openai_path_output_contract(env, monkeypatch, fake_openai, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    assert env() == 0
    out = capsys.readouterr()
    assert out.out == "OpenAI brief.\n"
    assert "brief_ai: openai gpt-5.5 effort=medium in=10 out=5 total=15" in out.err
    req = fake_openai[1]
    assert req["model"] == "gpt-5.5" and req["input"] == PROMPT
    assert req["reasoning"] == {"effort": "medium"}


def test_openai_ignores_an_installed_anthropic_key(env, monkeypatch,
                                                   fake_openai, fake_anthropic):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    assert env() == 0
    assert fake_anthropic["calls"] == []


# --- the Anthropic path -------------------------------------------------------

def test_anthropic_path_output_contract(env, monkeypatch, fake_anthropic, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    assert env() == 0
    out = capsys.readouterr()
    # Text blocks only, in order; the thinking block is not part of the brief.
    assert out.out == "Claude brief.\nSecond block.\n"
    assert ("brief_ai: anthropic claude-opus-5-5 effort=medium "
            "in=20 out=7 total=27") in out.err
    req = fake_anthropic["calls"][0]
    assert req["model"] == "claude-opus-5-5"
    assert req["messages"] == [{"role": "user", "content": PROMPT}]
    assert req["output_config"] == {"effort": "medium"}
    assert req["fallbacks"] == "default"
    assert req["betas"] == ["server-side-fallback-2026-07-01"]
    assert fake_anthropic["client"]["api_key"] == "sk-a"


def test_both_providers_receive_the_same_prompt(env, monkeypatch,
                                                fake_openai, fake_anthropic):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    assert env("--provider", "openai") == 0
    assert env("--provider", "anthropic") == 0
    assert fake_openai[1]["input"] == \
        fake_anthropic["calls"][0]["messages"][0]["content"] == PROMPT


def test_brief_model_overrides_and_drops_unsupported_fallback(env, monkeypatch,
                                                              fake_anthropic):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setenv("BRIEF_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("BRIEF_EFFORT", "low")
    assert env() == 0
    req = fake_anthropic["calls"][0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["output_config"] == {"effort": "low"}
    assert "fallbacks" not in req and "betas" not in req


def test_anthropic_refusal_is_a_failed_ai_half(env, monkeypatch,
                                               fake_anthropic, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    fake_anthropic["message"] = types.SimpleNamespace(
        content=[], stop_reason="refusal",
        stop_details=types.SimpleNamespace(category="cyber"), usage=None)
    assert env() == 1
    out = capsys.readouterr()
    assert out.out == "" and "declined (category=cyber)" in out.err


def test_anthropic_truncation_is_logged_not_hidden(env, monkeypatch,
                                                   fake_anthropic, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    fake_anthropic["message"].stop_reason = "max_tokens"
    assert env() == 0
    assert "WARN output hit max_tokens" in capsys.readouterr().err


def test_anthropic_error_redacts_the_key(env, monkeypatch, fake_anthropic, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    fake_anthropic["raise"] = RuntimeError("bad key sk-secret")
    assert env() == 1
    out = capsys.readouterr()
    assert out.out == "" and "sk-secret" not in out.err and "<redacted>" in out.err


def test_missing_sdk_is_reported(env, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises
    assert env() == 1
    assert "anthropic SDK not installed" in capsys.readouterr().err
