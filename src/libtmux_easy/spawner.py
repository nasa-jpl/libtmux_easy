import multiprocessing
import time
import os
import psutil


def spawn_child(depth, max_depth):
    if depth >= max_depth:
        print(f"[Leaf] PID {os.getpid()} reached max depth. Sleeping...")
        time.sleep(10)
        return

    # Create the next layer
    p = multiprocessing.Process(target=spawn_child, args=(depth + 1, max_depth))
    p.start()

    print(f"[Layer {depth}] PID {os.getpid()} spawned Child PID {p.pid}")

    # Keep the parent alive so the tree doesn't collapse
    p.join()


if __name__ == "__main__":
    layers = 3
    print(f"[Root] Main Process PID: {os.getpid()}")

    # Start the chain
    main_child = multiprocessing.Process(target=spawn_child, args=(1, layers))
    main_child.start()

    # Give the OS a moment to register all processes
    time.sleep(1)

    # --- Integration with your psutil logic ---
    print("\n--- psutil Tree Discovery ---")
    root = psutil.Process(os.getpid())
    for child in root.children(recursive=True):
        # Checking threads inside our dummy processes
        threads = child.threads()
        print(
            f"Discovered: PID {child.pid} | Parent: {child.ppid()} | Threads: {len(threads)}"
        )

    main_child.join()
