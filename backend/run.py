import os
import uvicorn

if __name__ == "__main__":
    is_dev = os.getenv("ENV", "production").lower() == "development"
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=is_dev)

