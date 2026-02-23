import sys
import os

# Ensure the app root is on the path so `import garmin` works
sys.path.insert(0, os.path.dirname(__file__))

from api.server import app

if __name__ == "__main__":
    app.run()
