from langchain_core.tools import tool


def build_tools(ctx):
    @tool
    def get_roadmap_progress() -> dict:
        """Read the current saved roadmap and progress. Missing means no plan is saved."""
        return {'roadmap': ctx['student_profile'].get('roadmap')}

    @tool
    def generate_application_roadmap(request: str) -> dict:
        """Get evidence for an application/exam roadmap. Compose the plan as the agent, verify institutional dates with university tools, then persist with save_advising_artifact(kind='roadmap')."""
        from .advising_tools import profile_evidence
        return {'request': request, 'student': profile_evidence(ctx), 'existing_roadmap': ctx['student_profile'].get('roadmap'),
            'instruction': 'Build realistic milestones. Label suggested dates separately from sourced official deadlines.'}

    return [get_roadmap_progress, generate_application_roadmap]
