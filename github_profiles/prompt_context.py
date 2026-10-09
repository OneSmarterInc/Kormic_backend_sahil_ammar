"""Lossless prompt compaction; saved evidence and reports stay unchanged."""
import json


SOURCE_REFERENCE_NOTE = (
    'A source with excerpt_ref has exactly the same excerpt as that source ID in the '
    'same repository. Its own ID and path still identify its evidence. '
    'Never resolve references across repositories.'
)


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def compact_sources(sources):
    """Send identical excerpts once, retaining every evidence ID and path."""
    seen, result = {}, []
    for source in sources:
        row = {'id': source['id'], 'path': source['path']}
        excerpt = source['excerpt']
        if excerpt in seen:
            row['excerpt_ref'] = seen[excerpt]
        else:
            row['excerpt'] = excerpt
            seen[excerpt] = source['id']
        result.append(row)
    return result


def compact_contribution(contribution):
    # Commit URLs remain in saved reports; the reasoning needs the signal and its scope.
    return {key: value for key, value in contribution.items() if key != 'commit_urls'}


def compact_history(history):
    result = []
    for event in history:
        observation = event['result']
        if event['tool'] == 'read_files' and 'sources' in observation:
            # Canonical IDs, paths and excerpts are supplied once in `sources`.
            observation = {'sources': [
                {key: value for key, value in source.items() if key in ('id', 'path', 'error')}
                for source in observation['sources']]}
        elif event['tool'] == 'inspect_contributions':
            observation = compact_contribution(observation)
        row = {'tool': event['tool'], 'arguments': event['arguments'], 'result': observation}
        # Only adjacent identical observations are redundant; retain errors and chronology.
        if not result or row != result[-1]:
            result.append(row)
    return result
