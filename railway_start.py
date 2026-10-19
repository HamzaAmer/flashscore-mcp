import os
import subprocess
import sys
import time
import urllib.request


def wait_for_flashscore():
    url = "http://127.0.0.1:8000/health"

    for _ in range(60):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 300:
                    print("Flashscore API is ready on port 8000.")
                    return
        except Exception:
            pass

        time.sleep(1)

    raise RuntimeError("Flashscore API failed to start on port 8000.")


def main():
    # Keep Railway's public PORT for the MCP server.
    public_port = os.getenv("PORT", "3001")

    # Flashscore must always use its own internal port 8000.
    flashscore_env = os.environ.copy()
    flashscore_env["PORT"] = "8000"

    print("Starting Flashscore API on internal port 8000...")

    flashscore = subprocess.Popen(
        [sys.executable, "run.py"],
        env=flashscore_env,
    )

    try:
        wait_for_flashscore()

        # MCP uses Railway's public PORT.
        mcp_env = os.environ.copy()
        mcp_env["PORT"] = public_port
        mcp_env["FLASHSCORE_BASE_URL"] = "http://127.0.0.1:8000"

        print(f"Starting MCP server on public port {public_port}...")

        mcp = subprocess.Popen(
            [sys.executable, "mcp_server.py"],
            env=mcp_env,
        )

        exit_code = mcp.wait()
        raise SystemExit(exit_code)

    finally:
        if flashscore.poll() is None:
            flashscore.terminate()


if __name__ == "__main__":
    main()