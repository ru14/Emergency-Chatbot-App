# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Set up .env, check your inference setup one step at a time, then make one model call.

    uv run check.py            the first run makes .env and asks for your key
    uv run check.py --models   lists the models the endpoint serves, one per line

It uses the standard library only, so `python3 check.py` works too.
It never prints your key. The last line it prints is the model's reply.
"""
import contextlib
import getpass
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE, TEMPLATE = ROOT / ".env", ROOT / ".env.example"
BASE_URL = "https://api.akamai-inference.com/v1"
PROMPT = "Which US state is Seattle in? Answer in one short sentence."
before_key = None  # .env as it was before this run saved a key, so a rejected key can be taken out again


def ok(message):
    print(f"  ok    {message}")


def fail(message, fix):
    print(f"  FAIL  {message}")
    print(f"        fix: {fix}")
    sys.exit(1)


def parse(line):
    """(name, value) for a NAME=value line, else None. Drops a Windows \\r, quotes, and a comment after the value."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    name, value = line.split("=", 1)
    return name.strip(), value.split(" #", 1)[0].strip().strip("\"'")


def read_env(text):
    return dict(pair for line in text.splitlines() if (pair := parse(line)))


def ask_key():
    """Read the key hidden on a terminal, or one visible line from a pipe. Returns "" for no key."""
    hidden = sys.stdin.isatty()
    print("  Paste the key from event staff, then press Enter." + (" Nothing shows as you paste." if hidden else "\n  key:"),
          flush=True)  # a pipe, such as Git Bash without a console, still shows the key: prompt
    try:
        raw = getpass.getpass("  key: ") if hidden else sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        print()
        raw = ""
    raw = raw.replace("\x1b[200~", "").replace("\x1b[201~", "")  # paste markers some terminals add
    return "".join(raw.split()).strip("\"'")


def prepare_env():
    """Make .env from .env.example, add any line it lacks, and ask for the key if it is empty."""
    global before_key
    if ENV_FILE.is_file():
        ok(".env found")
    elif TEMPLATE.is_file():
        ENV_FILE.write_bytes(TEMPLATE.read_bytes())
        with contextlib.suppress(OSError):
            ENV_FILE.chmod(0o600)  # only your user can read the key it will hold
        ok(".env made from .env.example")
    else:
        fail(".env and .env.example not found", "clone the repo again, as 00-setup/README.md shows")
    text = ENV_FILE.read_bytes().decode("utf-8")  # bytes in and out, so line endings stay as they are
    newline = "\r\n" if "\r\n" in text else "\n"
    have = read_env(text)
    template = TEMPLATE.read_text(encoding="utf-8").splitlines() if TEMPLATE.is_file() else []
    missing = [line.strip() for line in template if (pair := parse(line)) and pair[0] not in have]
    if missing:
        text += ("" if not text or text.endswith(("\n", "\r")) else newline) + newline.join(missing) + newline
        ENV_FILE.write_bytes(text.encode("utf-8"))
        ok(f"added {', '.join(line.split('=', 1)[0] for line in missing)} to .env from .env.example")
    env = read_env(text)
    if env.get("AKAMAI_API_KEY"):
        ok(f"AKAMAI_API_KEY is set, {len(env['AKAMAI_API_KEY'])} characters (value hidden)")
        return env
    key = ask_key()
    if not key:
        fail("no key was given, but git, uv, and the clone work",
             "get your team's key from event staff, then run  uv run check.py  again")
    before_key = text.encode("utf-8")
    lines = text.splitlines(keepends=True)
    n = [i for i, line in enumerate(lines) if (parse(line) or ("",))[0] == "AKAMAI_API_KEY"][-1]
    lines[n] = f"AKAMAI_API_KEY={key}" + lines[n][len(lines[n].rstrip("\r\n")):]  # the line keeps its ending
    ENV_FILE.write_bytes("".join(lines).encode("utf-8"))
    ok(f"AKAMAI_API_KEY saved in .env, {len(key)} characters (value hidden)")
    if not key.startswith("sk_aka_"):
        print("        note: event keys start with sk_aka_, and this one does not")
    return {**env, "AKAMAI_API_KEY": key}


def request(what, url, key, body=None):
    """GET the URL, or POST when there is a body. Returns parsed JSON or stops with a fix."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error)["error"]["message"]
        except Exception:
            detail = error.reason
        if error.code == 401 and before_key is not None:
            ENV_FILE.write_bytes(before_key)  # so the next run asks for the key again
            fix = "check.py took that key out of .env. Run  uv run check.py  again and paste the whole key."
        elif error.code == 401:
            fix = "open .env in your code editor and paste the key after AKAMAI_API_KEY=. Keys start with sk_aka_."
        elif error.code == 404:
            fix = f"set AKAMAI_BASE_URL={BASE_URL} in .env"
        elif error.code == 429:
            fix = ("the account behind your key used its token budget for this minute. Everyone who shares "
                   "that account counts against it. Wait one minute, then run this again.")
        else:
            fix = "wait one minute and run this again. If it still fails, ask event staff."
        fail(f"{what} returned {error.code}: {detail}", fix)
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            fail(f"{what}: this Python cannot verify HTTPS certificates", "run it with uv instead: uv run check.py")
        fail(f"{what}: {error.reason}", "check your internet connection, then run this again")
    except (TimeoutError, OSError) as error:
        fail(f"{what}: {error}", "check your internet connection, then run this again")
    except ValueError:
        fail(f"{what} sent back a web page, not the model API",
             "if the Wi-Fi has a sign-in page, open any website in a browser first")


def main():
    print("Checking your inference setup\n")
    env = prepare_env()
    key = env["AKAMAI_API_KEY"]
    base = env.get("AKAMAI_BASE_URL", "").rstrip("/")
    if not base.endswith("/v1"):
        fail(f"AKAMAI_BASE_URL is {base!r} and does not end in /v1", f"set AKAMAI_BASE_URL={BASE_URL} in .env")
    ok(f"AKAMAI_BASE_URL is {base}")

    models = sorted(m["id"] for m in request("listing models", f"{base}/models", key)["data"])
    ok(f"the endpoint lists {len(models)} models")
    if "--models" in sys.argv[1:]:
        print("\n" + "\n".join(models))
        return

    model = env.get("CHAT_MODEL", "")
    if model not in models:
        fail(f"CHAT_MODEL={model!r} is not in that list", f"set CHAT_MODEL in .env to one of: {', '.join(models)}")
    ok(f"CHAT_MODEL {model} is in the list")

    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 60}
    answer = request(f"asking {model}", f"{base}/chat/completions", key, body)
    reply = " ".join((answer["choices"][0]["message"].get("content") or "").split())
    if not reply:
        fail(f"{model} sent back an empty reply", "run this again. If it repeats, ask event staff.")
    ok(f"{model} answered\n")
    print(reply)


if __name__ == "__main__":
    main()
