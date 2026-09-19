"""Real PostgreSQL checkpoint gate; no MemorySaver or model/provider calls."""
import os
from uuid import uuid4
from django.db import connection
from django.test import TransactionTestCase
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, MessagesState, START, END


class CheckpointWorkerPersistenceTests(TransactionTestCase):
    def test_two_workers_restart_and_thread_isolation(self):
        if connection.vendor != 'postgresql':
            if os.environ.get('REQUIRE_POSTGRES_CHECKPOINT_TEST') == 'true':
                self.fail('Checkpoint release gate requires real PostgreSQL')
            self.skipTest('Run in PostgreSQL CI; SQLite cannot verify persistence')
        from pure_multi_agent.runtime import _build_checkpointer
        thread = 'matrix-' + str(uuid4())
        other = 'matrix-' + str(uuid4())
        config = {'configurable': {'thread_id': thread}}
        other_config = {'configurable': {'thread_id': other}}
        savers = []
        def worker():
            saver = _build_checkpointer()
            savers.append(saver)
            graph = StateGraph(MessagesState)
            graph.add_node('answer', lambda state: {'messages': [AIMessage(content='Remembered: ' + state['messages'][0].content)]})
            graph.add_edge(START, 'answer'); graph.add_edge('answer', END)
            return saver, graph.compile(checkpointer=saver)
        try:
            first, graph_a = worker()
            graph_a.invoke({'messages': [HumanMessage(content='My goal is physics')]}, config)
            second, graph_b = worker()
            self.assertIsNot(first.conn, second.conn)
            reply = graph_b.invoke({'messages': [HumanMessage(content='What is my goal?')]}, config)
            self.assertEqual(reply['messages'][-1].content, 'Remembered: My goal is physics')
            self.assertEqual(len(reply['messages']), 4)
            self.assertIsNone(second.get_tuple(other_config))
            first.conn.close(); second.conn.close()
            _, restarted = worker()
            self.assertEqual(len(restarted.get_state(config).values['messages']), 4)
            restarted.invoke({'messages': [HumanMessage(content='A different student')]}, other_config)
            self.assertEqual(restarted.get_state(config).values['messages'][0].content, 'My goal is physics')
            savers[-1].delete_thread(thread)
            self.assertIsNone(savers[-1].get_tuple(config))
            self.assertIsNotNone(savers[-1].get_tuple(other_config))
        finally:
            for saver in savers:
                if not saver.conn.closed:
                    saver.delete_thread(thread); saver.delete_thread(other)
                    saver.conn.close()
