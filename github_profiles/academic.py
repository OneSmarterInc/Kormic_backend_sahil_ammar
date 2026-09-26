"""Explainable study directions from source-linked skills, not admissions scoring."""
import re

BRANCHES = [
    ('software', 'Software Engineering', r'django|flask|fastapi|react|software engineering|rest.*api|api development|typescript|javascript|web development',
     'Build on application development through software architecture, dependable systems, and engineering practice.',
     ['Algorithms and data structures', 'Software design and testing', 'A documented end-to-end project']),
    ('ai', 'Artificial Intelligence & Machine Learning', r'machine learning|deep learning|pytorch|tensorflow|scikit.learn|neural network|natural language processing|computer vision|\bllm\b|\bollama\b|retrieval.augmented|\blangchain\b',
     'Explore intelligent applications, learning algorithms, and the evaluation of AI systems.',
     ['Linear algebra, calculus, and probability', 'Training and evaluating a model on a held-out dataset', 'Responsible AI and reproducible experiments']),
    ('data', 'Data Science & Analytics', r'pandas|numpy|data analysis|data science|statistics|matplotlib|seaborn|plotly|data visualization|data engineering|\betl\b',
     'Extend data processing and analytical work into statistical modelling and data-driven investigation.',
     ['Probability and statistical inference', 'SQL and data preparation', 'An analysis with clear evaluation and conclusions']),
    ('security', 'Cybersecurity', r'penetration testing|vulnerability analysis|cryptography|malware|network security|threat detection|security auditing|digital forensics',
     'Develop specialist knowledge of securing systems, investigating threats, and evaluating vulnerabilities.',
     ['Networking and operating systems', 'Threat modelling and secure design', 'An ethical, reproducible security investigation']),
    ('systems', 'Distributed Systems & Cloud Computing', r'distributed systems|kubernetes|docker|cloud computing|microservices|\baws\b|\bazure\b|terraform|message queue',
     'Build on infrastructure work through scalable services, distributed computing, and reliability.',
     ['Operating systems and networking', 'Concurrency and distributed algorithms', 'Measured scalability and failure-recovery experiments']),
    ('embedded', 'Embedded Systems & Robotics', r'robotics|embedded systems|arduino|microcontroller|\bros2?\b|sensor fusion|firmware',
     'Connect software with physical devices, sensing, control, and autonomous systems.',
     ['Control theory and electronics', 'Real-time programming', 'A measured hardware or simulation project']),
]


def recommend_masters(profile, projects):
    candidates = []
    for key, title, pattern, rationale, preparation in BRANCHES:
        evidence, score, seen = [], 0, set()
        for project in projects:
            identity = project.get('github_id') or project.get('full_name') or project.get('id')
            if identity in seen:
                continue
            seen.add(identity)
            analysis = project.get('analysis') or {}
            source_ids = {s.get('id') for s in analysis.get('sources', [])}
            matches = sorted({s['name'] for s in analysis.get('skills', [])
                if s.get('evidence_ids') and set(s['evidence_ids']) <= source_ids
                and re.search(pattern, s.get('name', ''), re.I)})
            if not matches:
                continue
            owned = (project.get('owner_login') or '').lower() == (profile.get('login') or '').lower()
            weight = 1 if owned and not project.get('fork') else .35
            score += weight  # One vote per project: repeated skill names cannot inflate priority.
            evidence.append({'project': project.get('full_name'), 'url': project.get('html_url'),
                'signals': matches, 'ownership': 'Fork' if project.get('fork') else 'Owned' if owned else 'Shared'})
        if evidence:
            candidates.append({'key': key, 'name': title, 'rationale': rationale,
                'preparation': preparation, 'evidence': evidence, 'project_count': len(evidence),
                'evidence_level': 'Repeated project signals' if score >= 2 else 'Early exploration signal',
                'note': ('Using AI models is different from training them; verify model-building and mathematical preparation.'
                         if key == 'ai' else 'Repository access and ownership do not establish individual authorship.'),
                '_score': score})
    candidates.sort(key=lambda item: (-item['_score'], item['name']))
    for rank, item in enumerate(candidates, 1):
        item.pop('_score')
        item['priority'] = rank
    return {'version': 1, 'recommendations': candidates,
        'method': 'Priority reflects the number of distinct projects with source-linked relevant skills. Owned non-fork projects count more than forks or shared projects. Equal evidence is ordered alphabetically. This is an indicative subject-fit ranking, not a measure of proficiency.',
        'scope': 'Use this shortlist alongside your interests, grades, mathematics preparation, and each university’s entry requirements. GitHub cannot establish admissions eligibility. Degree titles vary by university.',
        'empty_message': 'There is not enough source-linked specialist evidence to rank branches yet. Complete source analysis or add relevant projects; general programming languages alone do not establish a specialization.'}
