"""Entrypoint. Reads PORT from env directly — no shell expansion."""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8100"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
