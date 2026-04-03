"""Allow running: python -m dashboard"""

from dashboard.app import create_app

app = create_app()
app.run(host="127.0.0.1", port=5555, debug=False)
