import uvicorn
from crossexam.config import get_settings


def main():
    s = get_settings()
    uvicorn.run("crossexam.api.app:app", host=s.api_host, port=s.api_port)


if __name__ == "__main__":
    main()
