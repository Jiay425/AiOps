import uvicorn

from .config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run("ops_autoagent.api:app", host=settings.ops_host, port=settings.ops_port, reload=False)


if __name__ == "__main__":
    run()
