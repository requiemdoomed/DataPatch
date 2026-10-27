"""Start DataPatch locally.

    python app.py

then open http://127.0.0.1:5000 in a browser.
"""

import os

from datapatch.server import create_app

app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"DataPatch is running at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=debug)
