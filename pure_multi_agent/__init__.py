# pure_multi_agent
# A LangGraph-based multi-agent implementation of the student chat flow.
#
# The student's personal agent (main graph, built with
# langgraph.prebuilt.create_react_agent) dynamically decides which tool/agent
# to call -- GitHub analysis, verification, a specific university, or every
# university in parallel -- instead of a fixed keyword/intent-classifier
# dispatch chain. University agents are modeled as a real LangGraph subgraph
# (see university_graph.py) with their own state, reused unchanged from
# agents.university_agent.UniversityAgent under the hood.
#
# Entry point: pure_multi_agent.runtime.run_turn(student_id, message).
