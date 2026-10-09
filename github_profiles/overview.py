"""Evidence-grounded overview algorithm ported from GitHub AI core/sync.py."""
from collections import Counter

def factual_overview(profile, statistics, languages, projects, coverage, featured_ids=None):
    """Build the overview from saved source-backed findings without inference."""
    analyzed = [p for p in projects if p.get('analysis')]
    technologies = Counter(s['name'] for p in analyzed for s in p['analysis'].get('skills', []))
    domains = Counter(d for p in analyzed for d in p['analysis'].get('domains', []))
    name = profile.get('name') or profile['login']
    lines = ['## Professional Summary',
        f"**{name}** (@{profile['login']}) has a GitHub portfolio spanning **{len(projects)} selected projects**, "
        f"including **{statistics.get('owned', 0)} owned repositories** and **{statistics.get('forks', 0)} forks**. "
        f"Source-backed findings are available for **{len(analyzed)} projects**. "
        'This profile describes the technical work visible in those repositories, with ownership and contribution evidence kept separate.']
    if languages:
        lines.append('The most frequently detected languages are ' + ', '.join(f"**{r['name']}** ({r['repositories']} projects)" for r in languages[:5]) + '. Frequency indicates portfolio coverage, not a tested skill level.')
    lines.append('## Technical Expertise')
    for technology, count in technologies.most_common(10):
        lines.append(f'- **{technology}** — supported by inspected source in {count} projects.')
    if not technologies:
        lines.append('Source-backed technology findings will appear as analysis completes.')
    if domains:
        lines.append('The analyzed projects suggest work in ' + ', '.join(f'**{domain}** ({count} projects)' for domain,count in domains.most_common(5)) + '. These are inferred project domains, not verified professional specializations.')
    lines.append('## Project Experience')
    ordered = sorted(analyzed, key=lambda p: (p.get('owner_login') != profile['login'], bool(p.get('fork'))))
    if featured_ids:
        rank = {value: index for index, value in enumerate(featured_ids)}
        ordered.sort(key=lambda p: rank.get(p.get('id'), len(rank)))
    for project in ordered[:6]:
        label = 'fork' if project.get('fork') else 'owned repository' if project.get('owner_login') == profile['login'] else 'shared repository'
        summary = project['analysis'].get('summary', '')
        lines.append(f"- **[{project['full_name']}]({project.get('html_url') or 'https://github.com/'+project['full_name']})** ({label}): {summary}")
    if len(projects) > 6:
        lines.append('These are selected examples; the project portfolio below includes every saved repository and its individual findings.')
    lines.extend(['## Collaboration and Contribution',
        f"{len(projects)-statistics.get('owned', 0)} selected repositories are owned by other accounts or organizations. "
        'Shared access does not establish personal authorship. Each project lists available account-linked commit signals; those samples do not measure total contribution or code quality.',
        '## Evidence and Scope', coverage.get('note', ''),
        'Employment, education, certifications, years of experience, and deployment outcomes are not inferred from repository access.'])
    return '\n\n'.join(lines)


def synthesize_overview(profile, statistics, languages, projects, coverage, chat=None):
    """Deterministic presentation; the legacy chat argument is never invoked."""
    overview = factual_overview(profile, statistics, languages, projects, coverage)
    analyzed = [project for project in projects if project.get('analysis')]
    if not analyzed:
        return overview
    suggestions = [
        'For selected portfolio projects, consider documenting the problem, architecture, setup steps, and your specific contribution.',
        'Consider adding reproducible tests and reporting their actual results for representative projects; this analysis did not execute tests.',
    ]
    if any(p.get('fork') or p.get('owner_login', '').lower() != profile['login'].lower() for p in analyzed):
        suggestions.append('For shared and forked projects, explain which features or changes you personally implemented, with links to relevant commits or pull requests.')
    else:
        suggestions.append('Consider adding screenshots, a short project walkthrough, and clearly labeled outcomes to your strongest project READMEs.')
    overview += '\n\n## Development Opportunities\n\n' + '\n'.join('- ' + suggestion for suggestion in suggestions)
    return overview
