from .replay import replay
import json

if __name__ == "__main__":
    print(json.dumps(replay(), indent=2, default=str))
