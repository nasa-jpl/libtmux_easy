import psutil
import subprocess, os, time


def get_child_pids(parent_pid: int, recursive=False):
    """
    Finds the PIDs of all child processes of a given parent PID.

    :param parent_pid: The PID of the parent process.
    :param recursive: If True, includes children of children, and so on.
    :return: A list of child PIDs (integers).
    """
    try:
        # Get the Process object for the parent PID
        parent = psutil.Process(parent_pid)

        # Get the children. Pass recursive=True to include the entire process tree.
        children = parent.children(recursive=recursive)

        for child in children:
            print(
                f"Child PID: {child.pid}, Name: {child.name()}, Status: {child.status()}, Running: {child.is_running()}"
            )

        # children = [
        #     child for child in parent.children(recursive=True)
        #     if child.is_running() and child.status() != psutil.STATUS_ZOMBIE
        # ]

        breakpoint()

        # Extract and return the PIDs
        child_pids = [p.pid for p in children]
        return child_pids

    except psutil.NoSuchProcess:
        print(f"Error: Process with PID {parent_pid} does not exist.")
        return []
    except Exception as e:
        print(f"An error occurred: {e}")
        return []


if __name__ == "__main__":
    current_pid = psutil.Process().pid
    print(f"Current PID: {current_pid}")

    devnull = open(os.devnull, "wb")
    procs = []
    for _ in range(2):
        procs.append(
            subprocess.Popen(
                ["watch", "htop"],
                stdin=devnull,
                stdout=devnull,
                stderr=devnull,
                start_new_session=True,
                close_fds=True,
            )
        )
    devnull.close()
    time.sleep(0.1)

    child_pids = get_child_pids(current_pid, recursive=False)
    print(f"Child PIDs: {child_pids}")
    input("Press Enter to kill child processes...")
