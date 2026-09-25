#!/usr/bin/env python3
"""Entry point to launch the XML & XSD Interactive Graph Explorer server."""

import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Starting XML & XSD Interactive Graph Explorer at http://127.0.0.1:{port}")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
