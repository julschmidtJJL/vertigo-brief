#!/usr/bin/env python3
"""The AI half of the daily brief, via the OpenAI or the Anthropic API.

Uses an API key rather than a CLI login on purpose: a CLI's OAuth session
expires on a schedule nobody controls, which a scheduled job cannot survive.
An API key is a static credential that either works or fails loudly.

Provider: BRIEF_AI_PROVIDER=openai|anthropic. Unset, it follows whichever key
is present, OpenAI first, so a setup that had only an OpenAI key is unchanged.

Keys: ~/.config/vertigo/openai.env or anthropic.env (600), or OPENAI_API_KEY /
ANTHROPIC_API_KEY in the environment.

Both providers get the same prompt and return plain text on stdout. The
provenance marking (`AI` vs `ƒ`) and the rule that the model states no number
Python did not hand it live in the prompt and in run_daily_brief.sh, not here,
so they do not depend on which model wrote the text.

Usage:  brief_ai.py PROMPT_FILE [--provider openai] [--model gpt-5.5] [--effort medium]
        writes the brief to stdout, usage and errors to stderr
"""
import argparse, os, sys, time

CONFIG_DIR = os.path.expanduser("~/.config/vertigo")

PROVIDERS = {
    # provider: (key variable, key file, default model)
    "openai": ("OPENAI_API_KEY", "openai.env", "gpt-5.5"),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic.env", "claude-opus-5-5"),
}

# Claude models that accept the server-side refusal fallback. A declined
# request is then re-run on another model inside the same call instead of
# costing the run its AI half. Any other model is called without it.
ANTHROPIC_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5",
                             "claude-fable-5-1", "claude-sonnet-5-5"}


def load_key(var="OPENAI_API_KEY", path=None):
    path = path or os.path.join(CONFIG_DIR, PROVIDERS["openai"][1])
    if os.environ.get(var):
        return os.environ[var], None
    if not os.path.exists(path):
        return None, f"no {var} in the environment and no {path}"
    if os.stat(path).st_mode & 0o077:
        return None, f"{path} is readable by others (chmod 600 it)"
    for line in open(path):
        line = line.strip()
        if line.startswith(f"{var}="):
            return line.split("=", 1)[1].strip().strip('"\''), None
    return None, f"no {var} line in {path}"


def pick_provider(requested=None, config_dir=None):
    """(provider, key, error). An empty BRIEF_AI_PROVIDER counts as unset,
    because an unset Actions variable arrives as an empty string."""
    config_dir = config_dir or CONFIG_DIR
    requested = (requested or "").strip().lower()
    if requested:
        if requested not in PROVIDERS:
            return None, None, (f"unknown provider {requested!r} "
                                f"(use {' or '.join(PROVIDERS)})")
        var, fname, _ = PROVIDERS[requested]
        key, err = load_key(var, os.path.join(config_dir, fname))
        return requested, key, err
    errors = []
    for name, (var, fname, _) in PROVIDERS.items():
        key, err = load_key(var, os.path.join(config_dir, fname))
        if key:
            return name, key, None
        errors.append(err)
    return None, None, "no API key found: " + "; ".join(errors)


def call_openai(key, prompt, model, effort, timeout):
    from openai import OpenAI
    client = OpenAI(api_key=key, timeout=timeout, max_retries=2)
    r = client.responses.create(model=model, input=prompt,
                                reasoning={"effort": effort})
    u = getattr(r, "usage", None)
    usage = (u.input_tokens, u.output_tokens) if u else None
    return (r.output_text or "").strip(), usage, None


def call_anthropic(key, prompt, model, effort, timeout):
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=2)
    kwargs = dict(model=model, max_tokens=64000,
                  messages=[{"role": "user", "content": prompt}],
                  output_config={"effort": effort})
    if model in ANTHROPIC_FALLBACK_MODELS:
        kwargs.update(betas=["server-side-fallback-2026-07-01"],
                      fallbacks="default")
    # Streamed so a long brief never runs into the SDK's HTTP timeout for
    # large max_tokens; only the final message is used.
    with client.beta.messages.stream(**kwargs) as stream:
        r = stream.get_final_message()
    if r.stop_reason == "refusal":
        d = getattr(r, "stop_details", None)
        return "", None, f"model declined (category={getattr(d, 'category', None)})"
    text = "\n".join(b.text for b in r.content if b.type == "text").strip()
    warn = "output hit max_tokens and is cut off" if r.stop_reason == "max_tokens" else None
    u = getattr(r, "usage", None)
    usage = (u.input_tokens, u.output_tokens) if u else None
    return text, usage, warn


CALLS = {"openai": call_openai, "anthropic": call_anthropic}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--provider", default=os.environ.get("BRIEF_AI_PROVIDER"))
    ap.add_argument("--model", default=os.environ.get("BRIEF_MODEL") or None)
    ap.add_argument("--effort", default=os.environ.get("BRIEF_EFFORT") or "medium")
    ap.add_argument("--timeout", type=int, default=540)
    a = ap.parse_args()

    provider, key, err = pick_provider(a.provider)
    if err:
        print(f"brief_ai: {err}", file=sys.stderr)
        return 1
    try:
        prompt = open(a.prompt).read()
    except Exception as e:
        print(f"brief_ai: cannot read prompt: {e}", file=sys.stderr)
        return 1
    if not prompt.strip():
        print("brief_ai: prompt is empty", file=sys.stderr)
        return 1

    try:
        __import__(provider)
    except ImportError:
        print(f"brief_ai: {provider} SDK not installed", file=sys.stderr)
        return 1

    # The model is PINNED by name here rather than left to a CLI default.
    # A CLI resolves its own model, so an upgrade could have changed the brief's
    # quality with no signal — the exact silent drift this system avoids.
    model = a.model or PROVIDERS[provider][2]
    t0 = time.time()
    try:
        text, usage, problem = CALLS[provider](key, prompt, model, a.effort,
                                               a.timeout)
    except Exception as e:
        msg = str(e).replace(key, "<redacted>") if key else str(e)
        print(f"brief_ai: {type(e).__name__}: {msg[:400]}", file=sys.stderr)
        return 1

    if problem and not text:
        print(f"brief_ai: {problem}", file=sys.stderr)
        return 1
    if problem:
        print(f"brief_ai: WARN {problem}", file=sys.stderr)
    if not text:
        print("brief_ai: model returned no text", file=sys.stderr)
        return 1

    if usage:
        # Straight into daily-brief.log so per-run cost is measurable from the
        # log alone, which is the open question this migration turns on.
        i, o = usage
        print(f"brief_ai: {provider} {model} effort={a.effort} "
              f"in={i} out={o} total={i + o} {time.time()-t0:.0f}s",
              file=sys.stderr)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
