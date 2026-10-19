"""One assembly path for CLI, offline browser demo and integration tests."""

from datetime import datetime
from pathlib import Path
import uuid

from .browser import BrowserSession
from .config import Settings, validate_url
from .replay import run_replay
from .reporting import write_report
from .workflow import build_graph


async def run_test(settings: Settings, *, query: str, target_url: str, planner,
                   sample_site: bool = False, browser_factory=BrowserSession,
                   replay_runner=run_replay, progress=None, run_dir: Path | None = None) -> dict:
    target_url = validate_url(target_url)
    if not query.strip():
        raise ValueError("测试需求不能为空。")
    run_dir = run_dir or settings.runtime_path / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    state = dict(query=query.strip(), target_url=target_url, sample_site=sample_site,
                 actions=[], index=0, snippets=[], events=[], html="", status="running", phase="plan", error="")
    browser = browser_factory(settings, run_dir)
    try:
        graph = build_graph(settings, planner, browser, run_dir, replay_runner)
        async for update in graph.astream(state, config={"recursion_limit": settings.max_actions * 4 + 10},
                                          stream_mode="updates"):
            for node, values in update.items():
                state.update(values)
                if progress:
                    progress(node, state)
    except Exception as error:
        state.update(status="error", error=f"{type(error).__name__}: {settings.redact(str(error))}")
    except BaseException:
        state.update(status="error", error="本次运行被中断。")
        raise
    finally:
        try:
            await browser.close()
        finally:
            if state["snippets"] and not (run_dir / "test_generated.py").exists():
                (run_dir / "partial_steps.py").write_text(
                    "# Incomplete snippets for diagnosis; not a complete test.\n" + "\n\n".join(state["snippets"]),
                    encoding="utf-8")
            result = write_report(run_dir, state, settings)
    return result
