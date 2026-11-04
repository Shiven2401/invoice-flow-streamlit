"""Start Invoice Flow:  python run.py   then open the link it prints (http://localhost:8000)

Environment variables (all optional; they can also go in a .env file):
  GEMINI_API_KEY      turn on the Google Gemini vision extractor (best for scans, photos, handwriting)
  GEMINI_MODEL        Gemini model (default gemini-3.5-flash-lite)
  ANTHROPIC_API_KEY   or use Claude instead (an sk-ant- key); Gemini is used if both are set
  EXTRACTOR           auto (default) | llm | rules
  EXTRACT_MODEL       Claude model (default claude-sonnet-5-5)
  STAGE_DELAY         seconds to pause between stages so a human can watch (default 0.35, use 0 for speed)
  AS_OF_DATE          pretend 'today' is this date, YYYY-MM-DD (invoice-date sanity checks)
  HOST                default 127.0.0.1
  PORT                default 8000
  LOG_LEVEL           console log level (default INFO); runtime/logs/invoice-flow.log always keeps DEBUG
  COOKIE_SECURE       1 when served over HTTPS, so login cookies are never sent in the clear
  ALLOW_SIGNUP        1 (default) lets anyone create an account, 0 turns that off
  MAX_UPLOAD_MB       largest upload in MB (default 25)
  MAX_QUEUED_PER_USER, MAX_RUNS_PER_USER   0 = no limit (default); set them when other people share the server
  SESSION_DAYS        how long a login lasts (default 14)
"""
import os
import warnings

warnings.filterwarnings("ignore")
import uvicorn


class Server(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)  # exits if the port can't be bound, so a printed link is always live
        c = self.config
        host = "localhost" if c.host in ("127.0.0.1", "0.0.0.0", "::") else c.host
        print(f"\n  Invoice Flow is running at  http://{host}:{c.port}\n  Press Ctrl+C to stop\n", flush=True)


if __name__ == "__main__":
    Server(uvicorn.Config("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")),
                          log_level="warning", log_config=None)).run()
