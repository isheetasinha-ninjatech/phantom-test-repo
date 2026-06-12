## Quick Reference

**Slack Commands:**
- `python slack_interface.py read -l 50` - Read recent messages
- `python slack_interface.py say "message"` - Post updates
- `python slack_interface.py upload <file> --title "..."` - Upload file/screenshot
- `python slack_interface.py config` - Check configuration

**Browser (Persistent — tabs survive between tasks):**
- `python ninja/browser_server.py status` - Check browser status
- `python ninja/browser_server.py start` - Start browser if not running
- Connect in Python: `BrowserInterface.connect_cdp()` (see your spec for full API)

**Tavily Web Research** (text-based, no browser needed):
- `from tavily_client import Tavily; t = Tavily()` - Initialize
- `t.search("query")`, `t.extract(["url"])`, `t.crawl("url")`, `t.research("topic")`

**Pipedream Integrations:**
- `tools/pdx.py` installs as `pdx`; see `agent-docs/PIPEDREAM_CONNECT.md` for OAuth dashboard, connected app discovery, and actions.

---

## Headless Mode

You are running in **headless CLI mode** — there is no human at the terminal.

**Communicate via Slack only** using `python slack_interface.py`.

## Slack Communication Protocol

- **Keep messages SHORT** — 2-4 sentences max. No walls of text. Be direct.
- **Reply in threads** — If someone asks you a question or requests an update, reply in the thread (`-t thread_ts`), not as a new message.

**Workflow:**
1. Read your spec file first: `cat agent-docs/NINJA_SPEC.md`
2. Read Slack for new requests or context
3. Do your work (browser tasks, research, screenshots, data extraction)
4. Post results to Slack (short messages, attach screenshots/files)
5. Commit any code changes to git
6. Update your memory file (`memory/ninja_memory.md`)

---

## Reflect & Improve Toolkit

After completing your main task, **reflect on your workflow** and look for improvement opportunities:

1. **Analyze what was hard** — Did any step require too many manual commands? Was there repetitive boilerplate?
2. **Check `tools/` directory** — Run `ls tools/` to see existing helper tools. Read `tools/README.md` for what's available.
3. **Identify gaps** — Think about what reusable tool would have saved you time. Examples:
   - A common multi-step operation you keep repeating
   - A validation/check you run manually that could be automated
   - A data extraction or formatting pattern you use often
4. **Build new tools** — If you identify a useful tool:
   - Create it in `tools/<name>.py` with a CLI interface (`if __name__ == "__main__"`)
   - Include a docstring explaining usage
   - Add both a Python API and CLI entry point
   - Test it works: `python tools/<name>.py --help` or `python tools/<name>.py <test_args>`
5. **Update the registry** — Add your new tool to `tools/README.md` so future sessions know about it.
6. **Organize** — If you see loose scripts in the project root that belong in `tools/`, move them and update imports.

**Tool Design Principles:**
- Each tool should do ONE thing well
- Always support `--help` with clear usage examples
- Return structured output (JSON when possible) for composability
- Include error handling and helpful error messages
- Keep tools independent — minimal cross-dependencies

**Existing tools to be aware of:**
- `tools/health_check.py` — System health (browser, Slack, GitHub, settings)
- `tools/log_analyzer.py` — Parse Claude Code JSONL logs for cost/errors
- `tools/stealth_audit.py` — Browser stealth verification
- `tools/session_manager.py` — Browser session save/restore
- `tools/message_sanitizer.py` — Strip LLM artifacts from text

---
