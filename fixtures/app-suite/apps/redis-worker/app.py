import time


if __name__ == "__main__":
    while True:
        print("fixture-redis-worker tick", flush=True)
        time.sleep(60)

