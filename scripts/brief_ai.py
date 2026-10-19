#!/usr/bin/env python3
"""The AI half of the daily brief, via the OpenAI API.

Uses an API key rather than a CLI login on purpose: a CLI's OAuth session
expires on a schedule nobody controls, which a scheduled job cannot survive.
An API key is a static credential that either works or fails loudly.

Key: ~/.config/vertigo/openai.env (600), or OPENAI_API_KEY in the environment.

Usage:  brief_ai.py PROMPT_FILE [--model gpt-5.5] [--effort medium]
        writes the brief to stdout, usage and errors to stderr
"""
import argparse, os, sys, time

ENV_FILE = os.path.expanduser("~/.config/vertigo/openai.env")


def load_key(path=ENV_FILE):
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"], None
    if not os.path.exists(path):
        return None, f"no key in the environment and no {path}"
    if os.stat(path).st_mode & 0o077:
        return None, f"{path} is readable by others (chmod 600 it)"
    for line in open(path):
        line = line.strip()
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"\''), None
    return None, f"no OPENAI_API_KEY line in {path}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--model", default=os.environ.get("BRIEF_MODEL", "gpt-5.5"))
    ap.add_argument("--effort", default=os.environ.get("BRIEF_EFFORT", "medium"))
    ap.add_argument("--timeout", type=int, default=540)
    a = ap.parse_args()

    key, err = load_key()
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
        from openai import OpenAI
    except ImportError:
        print("brief_ai: openai SDK not installed", file=sys.stderr)
        return 1

    # The model is PINNED by name here rather than left to a CLI default.
    # A CLI resolves its own model, so an upgrade could have changed the brief's
    # quality with no signal — the exact silent drift this system avoids.
    t0 = time.time()
    try:
        client = OpenAI(api_key=key, timeout=a.timeout, max_retries=2)
        r = client.responses.create(model=a.model, input=prompt,
                                    reasoning={"effort": a.effort})
    except Exception as e:
        msg = str(e).replace(key, "<redacted>") if key else str(e)
        print(f"brief_ai: {type(e).__name__}: {msg[:400]}", file=sys.stderr)
        return 1

    text = (r.output_text or "").strip()
    if not text:
        print("brief_ai: model returned no text", file=sys.stderr)
        return 1

    u = getattr(r, "usage", None)
    if u:
        # Straight into daily-brief.log so per-run cost is measurable from the
        # log alone, which is the open question this migration turns on.
        print(f"brief_ai: {a.model} effort={a.effort} "
              f"in={u.input_tokens} out={u.output_tokens} "
              f"total={u.total_tokens} {time.time()-t0:.0f}s", file=sys.stderr)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
