#!/usr/bin/env python3
"""statusline-vocab installer — install / status / uninstall.

Idempotent. Safe to re-run. The skill's SKILL.md documents the design; this
script just executes it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
HOOK_DIR = CLAUDE / "hooks" / "vocab"
HOOK_SCRIPT = HOOK_DIR / "extract.sh"
VOCAB_DIR = CLAUDE / "vocab"
VOCAB_CONFIG = VOCAB_DIR / "config"
SETTINGS = CLAUDE / "settings.json"
STATUSLINE = CLAUDE / "statusline-command.sh"

DEFAULT_LANG = "Chinese"

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_EXTRACT = SCRIPT_DIR / "extract.sh"
SRC_DEFAULT_STATUSLINE = SCRIPT_DIR / "default-statusline.sh"

HOOK_CMD = str(HOOK_SCRIPT)  # absolute path used to dedupe settings entries
VOCAB_MARKER = "# vocab:start"  # marker we drop in the statusline

# Snippet printed for users with a custom statusline so they (or Claude in
# the conversation) can integrate manually. Kept here verbatim and also
# documented in SKILL.md so the two stay in sync visually.
SNIPPET = r"""# vocab:start — statusline-vocab skill
vocab_file="$HOME/.claude/vocab/current.json"
vocab_part=""
if [ -f "$vocab_file" ]; then
  v_word=$(jq -r '.word // empty' "$vocab_file" 2>/dev/null)
  if [ -n "$v_word" ] && [ "$v_word" != "null" ]; then
    v_emoji=$(jq -r '.emoji // "📖"' "$vocab_file" 2>/dev/null)
    v_ipa=$(jq -r '.ipa // ""' "$vocab_file" 2>/dev/null)
    v_pos=$(jq -r '.pos // ""' "$vocab_file" 2>/dev/null)
    v_meaning=$(jq -r '.meaning // ""' "$vocab_file" 2>/dev/null)
    vocab_part=" · ${v_emoji} \033[1;94m${v_word}\033[0m \033[2m${v_ipa}\033[0m \033[0;33m${v_pos}\033[0m ${v_meaning}"
  fi
fi
# vocab:end
"""


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))


def read_config() -> dict[str, str]:
    """Read ~/.claude/vocab/config (key=value per line). Missing file → {}."""
    if not VOCAB_CONFIG.exists():
        return {}
    out = {}
    for line in VOCAB_CONFIG.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_config(cfg: dict[str, str]) -> None:
    VOCAB_DIR.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n"
    VOCAB_CONFIG.write_text(body)


def current_lang() -> str:
    return read_config().get("lang", DEFAULT_LANG)


def set_lang(lang: str) -> None:
    cfg = read_config()
    cfg["lang"] = lang
    write_config(cfg)


def _validate_lang_or_exit(raw: str) -> str:
    """Strip the input and reject empty values. argparse won't catch `--lang ""`
    or `--lang "   "`, and writing an empty value would produce a broken prompt
    ("for a -speaking learner")."""
    cleaned = (raw or "").strip()
    if not cleaned:
        print("error: --lang must be a non-empty string (e.g. Chinese, Japanese, Spanish)",
              file=sys.stderr)
        sys.exit(2)
    return cleaned


def check_prereqs() -> list[str]:
    issues = []
    if not shutil.which("claude"):
        issues.append(
            "`claude` CLI not found on PATH — required by the Stop hook")
    if not shutil.which("jq"):
        issues.append("`jq` not found on PATH — required by hook + statusline")
    if not CLAUDE.is_dir():
        issues.append(
            f"{CLAUDE} does not exist — has Claude Code ever run on this machine?")
    return issues


def install_hook_script() -> None:
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    VOCAB_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC_EXTRACT, HOOK_SCRIPT)
    HOOK_SCRIPT.chmod(0o755)
    check(f"hook script at {HOOK_SCRIPT}", True)


def load_settings() -> dict:
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text())
        except json.JSONDecodeError as e:
            print(
                f"  ! {SETTINGS} is not valid JSON ({e}). Aborting.", file=sys.stderr)
            sys.exit(2)
    return {}


def save_settings(data: dict, suffix: str) -> None:
    if SETTINGS.exists():
        backup = SETTINGS.with_name(SETTINGS.name + suffix)
        shutil.copy2(SETTINGS, backup)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def register_hook() -> None:
    data = load_settings()
    hooks = data.setdefault("hooks", {})
    stop_entries = hooks.setdefault("Stop", [])

    # Find the matcher="" block, or create one. (Claude Code allows multiple
    # blocks per event keyed by matcher; matcher="" means "always run".)
    block = None
    for entry in stop_entries:
        if entry.get("matcher", "") == "":
            block = entry
            break
    if block is None:
        block = {"matcher": "", "hooks": []}
        stop_entries.append(block)

    inner = block.setdefault("hooks", [])
    for h in inner:
        if h.get("command") == HOOK_CMD:
            check(f"hook registered in {SETTINGS}", True, "already present")
            return

    inner.append({
        "type": "command",
        "command": HOOK_CMD,
        "timeout": 30,
        "async": True,
    })
    save_settings(data, ".bak-vocab")
    check(f"hook registered in {SETTINGS}",
          True, "added; backup at *.bak-vocab")


def install_statusline() -> str:
    """Returns one of: 'installed-default', 'already', 'manual-needed'."""
    if not STATUSLINE.exists() or STATUSLINE.read_text().strip() == "":
        STATUSLINE.write_text(SRC_DEFAULT_STATUSLINE.read_text())
        STATUSLINE.chmod(0o755)
        check(f"statusline installed at {STATUSLINE}",
              True, "wrote bundled default")
        return "installed-default"

    content = STATUSLINE.read_text()
    if VOCAB_MARKER in content:
        check(f"statusline at {STATUSLINE}", True,
              "vocab section already present")
        return "already"

    check(f"statusline at {STATUSLINE}", False,
          "custom file detected — manual step needed")
    print()
    print("  Your statusline already exists and the installer will NOT auto-modify it.")
    print("  Read the file, then insert the snippet below somewhere that lets `$vocab_part`")
    print("  land at the end of the rendered line. The skill's SKILL.md (Step 3) has the")
    print("  three common integration shapes.")
    print()
    print("  ---- snippet ----")
    for ln in SNIPPET.splitlines():
        print(f"  {ln}")
    print("  -----------------")
    return "manual-needed"


def cmd_install(lang: str | None = None) -> None:
    issues = check_prereqs()
    if issues:
        print("Prerequisites missing:", file=sys.stderr)
        for i in issues:
            print(f"  - {i}", file=sys.stderr)
        sys.exit(1)

    # Validate --lang BEFORE making any filesystem changes. A failure here must
    # leave the system untouched — partial installs from bad input violate the
    # installer's idempotency / transactional contract.
    if lang is not None:
        lang = _validate_lang_or_exit(lang)

    print("Installing statusline-vocab…")
    install_hook_script()
    register_hook()
    statusline_state = install_statusline()
    if lang is not None:
        set_lang(lang)
        check(f"language set to {lang}", True, f"{VOCAB_CONFIG}")
    elif not VOCAB_CONFIG.exists():
        # First install with no flag → write default so the user can see/edit it.
        set_lang(DEFAULT_LANG)
        check(f"language defaulted to {DEFAULT_LANG}",
              True, f"wrote {VOCAB_CONFIG}")
    print()
    print(
        f"target language: {current_lang()}  (change with: install.py config --lang <name>)")
    print()
    if statusline_state == "manual-needed":
        print("Almost done — finish the statusline integration above, then run:")
    else:
        print("Done. Run a Claude Code session and within a few turns the statusline")
        print("will pick up its first word. To preview without waiting, see:")
    print(f"  python {SCRIPT_DIR}/install.py status")


def cmd_config(lang: str | None) -> None:
    if lang is None:
        # Show current — distinguish explicit vs default to match cmd_status output.
        explicit = VOCAB_CONFIG.exists() and "lang" in read_config()
        suffix = "" if explicit else "  (default — no config file yet)"
        print(f"target language: {current_lang()}{suffix}")
        print(f"config file:     {VOCAB_CONFIG}")
        return
    lang = _validate_lang_or_exit(lang)
    set_lang(lang)
    print(f"target language set to: {lang}")
    print(f"  written to {VOCAB_CONFIG}")
    print(f"  next Stop hook will use the new language (existing current.json unaffected)")


def cmd_status() -> None:
    print("statusline-vocab status")
    print()

    check(f"hook script at {HOOK_SCRIPT}", HOOK_SCRIPT.exists())

    registered = False
    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text())
            for entry in data.get("hooks", {}).get("Stop", []):
                for h in entry.get("hooks", []):
                    if h.get("command") == HOOK_CMD:
                        registered = True
        except json.JSONDecodeError:
            pass
    check(f"hook registered in {SETTINGS}", registered)

    integrated = STATUSLINE.exists() and VOCAB_MARKER in STATUSLINE.read_text()
    check(f"statusline integrated ({STATUSLINE})", integrated)

    print()
    print(f"target language: {current_lang()}"
          + ("" if VOCAB_CONFIG.exists() else "  (default — no config file yet)"))

    print()
    current = VOCAB_DIR / "current.json"
    if current.exists():
        try:
            v = json.loads(current.read_text())
            word = v.get("word")
            if word:
                print(f"current word:   {v.get('emoji', '📖')}  {word}  {v.get('ipa', '')}  "
                      f"{v.get('pos', '')}  {v.get('meaning', '')}")
                print(f"  saved at:     {v.get('ts', 'unknown')}")
            else:
                print(
                    f"current word:   (null — extractor ran but found no suitable word)")
        except json.JSONDecodeError:
            print(f"current word:   (failed to parse {current})")
    else:
        print("current word:   none yet — fires after the next Stop hook")

    history = VOCAB_DIR / "history.jsonl"
    if history.exists():
        n = sum(1 for _ in history.open())
        print(f"history:        {n} entries  ({history})")
    else:
        print(f"history:        no history yet")


def cmd_uninstall() -> None:
    print("Uninstalling statusline-vocab…")

    # 1. Remove hook entry from settings.json
    if SETTINGS.exists():
        data = load_settings()
        changed = False
        for entry in data.get("hooks", {}).get("Stop", []):
            inner = entry.get("hooks", [])
            new = [h for h in inner if h.get("command") != HOOK_CMD]
            if len(new) != len(inner):
                entry["hooks"] = new
                changed = True
        # Prune now-empty matcher blocks we added
        data.get("hooks", {})["Stop"] = [
            e for e in data.get("hooks", {}).get("Stop", []) if e.get("hooks")
        ]
        if changed:
            save_settings(data, ".bak-vocab-uninstall")
            check(f"hook removed from {SETTINGS}",
                  True, "backup at *.bak-vocab-uninstall")
        else:
            check(f"hook entry in {SETTINGS}", True, "nothing to remove")

    # 2. Delete extract.sh
    if HOOK_SCRIPT.exists():
        HOOK_SCRIPT.unlink()
        check(f"removed {HOOK_SCRIPT}", True)
    else:
        check(f"hook script {HOOK_SCRIPT}", True, "already gone")

    # 3. Statusline: leave alone, instruct
    if STATUSLINE.exists() and VOCAB_MARKER in STATUSLINE.read_text():
        print()
        print(f"  ! {STATUSLINE} still contains a vocab section.")
        print(f"    Delete lines between `# vocab:start` and `# vocab:end` to clean up,")
        print(f"    or `rm` the file if it's the bundled default and you don't need it.")

    # 4. Vocab data dir stays
    if VOCAB_DIR.exists():
        print()
        print(f"  · preserved {VOCAB_DIR} (your generated 生词本 + history)")
        print(f"    `rm -rf {VOCAB_DIR}` to wipe.")


def main() -> None:
    parser = argparse.ArgumentParser(description="statusline-vocab installer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser(
        "install", help="install or re-install (idempotent)")
    p_install.add_argument(
        "--lang", help=f'translation language for the meaning field (default: {DEFAULT_LANG}). Examples: Chinese, Japanese, Spanish, French, German, Korean')

    sub.add_parser("status", help="report what's installed and current word")

    p_config = sub.add_parser(
        "config", help="change settings without reinstalling")
    p_config.add_argument("--lang", help="set the translation language")

    sub.add_parser(
        "uninstall", help="remove hook + script (preserves history)")

    args = parser.parse_args()

    if args.cmd == "install":
        cmd_install(lang=args.lang)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "config":
        cmd_config(lang=args.lang)
    elif args.cmd == "uninstall":
        cmd_uninstall()


if __name__ == "__main__":
    main()
