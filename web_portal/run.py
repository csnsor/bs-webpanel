import os

import uvicorn


def main() -> None:
    port_raw = os.getenv("PORT", "8000")
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 8000
    uvicorn.run("web_portal.main:app", host="0.0.0.0", port=port, proxy_headers=True)


if __name__ == "__main__":
    main()

