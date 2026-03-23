import pytest

from corpclaw_lite.memory.sqlite import SQLiteMemory


@pytest.fixture
def memory(tmp_path):
    # Use temporary file for testing
    db_file = tmp_path / "test_memory.db"
    return SQLiteMemory(str(db_file))


def test_add_and_get_message(memory):
    user_id = "user123"

    memory.add_message(user_id, "user", "Hello World")
    memory.add_message(user_id, "assistant", "Hi there!")

    history = memory.get_history(user_id)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Hello World"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Hi there!"


def test_history_limit(memory):
    user_id = "user123"
    for i in range(10):
        memory.add_message(user_id, "user", f"Message {i}")

    history = memory.get_history(user_id, limit=5)
    assert len(history) == 5
    # Since it orders DESC and then reverses, we get the last 5 inserted messages
    assert history[0]["content"] == "Message 5"
    assert history[-1]["content"] == "Message 9"


def test_clear_history(memory):
    user_id = "user456"
    memory.add_message(user_id, "user", "Test")
    assert len(memory.get_history(user_id)) == 1

    memory.clear(user_id)
    assert len(memory.get_history(user_id)) == 0


def test_dict_storage(memory):
    user_id = "user789"
    tool_call = {"name": "test_tool", "kwargs": {"a": 1}}
    memory.add_message(user_id, "tool", tool_call)

    history = memory.get_history(user_id)
    assert len(history) == 1
    assert history[0]["role"] == "tool"
    assert history[0]["content"] == tool_call
