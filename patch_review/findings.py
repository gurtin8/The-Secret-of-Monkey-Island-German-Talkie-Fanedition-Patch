"""Deterministic word alignment using the existing QA normalization."""
from audio_qa_v1.audit import normalize
from audio_qa_v1.date_equivalence import canonical_named_dates, dates_equivalent
from voice_anchor_wizard.text_rules import canonicalize_integer_numbers

CATEGORIES = [('one_word_wrong', 'Ein Wort falsch'), ('most_words_wrong', 'Alle/die meisten Wörter falsch'),
              ('single_word_error', 'Fehler im Einwortdialog'), ('missing_word', 'Wort fehlt'),
              ('extra_word', 'Wort zu viel'), ('no_words', 'Kein Wort erkannt')]
REASONS = {'technical_invalid': 'Die Audiodatei ist technisch ungültig.',
 'text_duration_outlier': 'Die Audiodauer ist für den Text auffällig.',
 'reference_duration_outlier': 'Die Audiodauer weicht auffällig vom Original ab.',
 'stt_unavailable': 'Die erforderliche Spracherkennung ist nicht verfügbar.',
 'stt_deferred_manual_review': 'Die Spracherkennung wurde zurückgestellt; eine manuelle Prüfung ist erforderlich.',
 'render_not_available': 'Es liegt keine erfolgreich gerenderte Ausgabe vor.',
 'technical_silence': 'Die Sprachaufnahme enthält nur Stille.',
 'technical_clipping': 'Die Aufnahme ist übersteuert.',
 'extreme_duration': 'Die Audiodauer liegt außerhalb der zulässigen technischen Grenzen.',
 'content_needs_classification': 'Der Inhalt muss vor der Sprachprüfung als Sprache oder Geräusch eingeordnet werden.',
 'number_mismatch': 'Die erkannte Zahl weicht von der erwarteten Zahl ab.',
 'number_unverified': 'Die erwartete Zahl wurde nicht bestätigt.',
 'short_text_mismatch': 'Die Spracherkennung weicht vom kurzen Solltext ab.',
 'missing_content_words': 'Die Spracherkennung bestätigt nicht alle Inhaltswörter.',
 'low_expected_coverage': 'Die Spracherkennung bestätigt nur einen kleinen Teil des Solltextes.',
 'many_extra_words': 'Die Spracherkennung enthält viele zusätzliche Wörter.',
 'asr_loop': 'Die Spracherkennung enthält auffällige Wiederholungen.',
 'clipping': 'Die Aufnahme ist übersteuert.', 'silence': 'Die Aufnahme enthält keine hörbare Sprache.'}


def align(expected, heard):
    a,b=normalize(canonicalize_integer_numbers(canonical_named_dates(expected),allow_sentence_final_period=True)).split(),normalize(canonicalize_integer_numbers(canonical_named_dates(heard),allow_sentence_final_period=True)).split()
    # Equal-cost alignments prefer preserved matching words over substitutions.
    dp=[[(0,0)]*(len(b)+1) for _ in range(len(a)+1)]
    for i in range(len(a)+1): dp[i][0]=(i,0)
    for j in range(len(b)+1): dp[0][j]=(j,0)
    for i in range(1,len(a)+1):
        for j in range(1,len(b)+1):
            same=a[i-1]==b[j-1]
            dp[i][j]=min((dp[i-1][j][0]+1,dp[i-1][j][1]),(dp[i][j-1][0]+1,dp[i][j-1][1]),(dp[i-1][j-1][0]+(not same),dp[i-1][j-1][1]-same))
    i,j=len(a),len(b); edits=[]
    while i or j:
        if i and j and dp[i][j]==(dp[i-1][j-1][0]+(a[i-1]!=b[j-1]),dp[i-1][j-1][1]-(a[i-1]==b[j-1])):
            if a[i-1]!=b[j-1]: edits.append(('replace',a[i-1],b[j-1]))
            i-=1;j-=1
        elif i and dp[i][j]==(dp[i-1][j][0]+1,dp[i-1][j][1]): edits.append(('missing',a[i-1],None));i-=1
        else: edits.append(('extra',None,b[j-1]));j-=1
    return dp[-1][-1][0],a,b,list(reversed(edits))


def word_findings(expected, heard, allowed=()):
    if any(dates_equivalent(form, heard) for form in [expected, *allowed]):return [],[]
    if any(normalize(form).replace(' ','')==normalize(heard).replace(' ','') for form in [expected,*allowed]):return [],[]
    _,a,b,edits=min((align(form,heard) for form in [expected,*allowed]),key=lambda v:v[0])
    replacements=[e for e in edits if e[0]=='replace'];missing=[e for e in edits if e[0]=='missing'];extra=[e for e in edits if e[0]=='extra']
    affected=len(replacements)+len(missing);categories=[];findings=[]
    if len(replacements)==1 and not missing and not extra:categories.append('one_word_wrong')
    if affected>len(a)/2 and a:categories.append('most_words_wrong');findings.append(f'{affected} von {len(a)} Sollwörtern sind ersetzt oder fehlen.')
    if len(a)==1 and edits:categories.append('single_word_error')
    if missing:categories.append('missing_word')
    if extra:categories.append('extra_word')
    if a and not b:categories.append('no_words')
    for kind,want,got in edits:
        findings.append(f'Erwartet „{want}“, erkannt „{got}“.' if kind=='replace' else f'Das erwartete Wort „{want}“ fehlt.' if kind=='missing' else f'Das zusätzliche Wort „{got}“ wurde erkannt.')
    return categories,findings


def integer_numbers_equivalent(expected, heard):
    """True when spelling only unambiguous integers makes full texts equal."""
    if dates_equivalent(expected, heard):
        return False
    def plain(value):
        value = canonicalize_integer_numbers(canonical_named_dates(value), allow_sentence_final_period=True)
        return normalize(value)
    return (plain(expected) != normalize(canonical_named_dates(expected)) or
            plain(heard) != normalize(canonical_named_dates(heard))) and plain(expected) == plain(heard)


def stt_state(record, valid=True, copy=False):
    stages=[dict(model=stage.get('stt_model'),stt_model=stage.get('stt_model'),status='completed' if stage.get('stt_status') in ('pass','warn') else stage.get('stt_status','absent'),stt_status=stage.get('stt_status'),transcript=stage.get('transcript') if stage.get('stt_status') in ('pass','warn') else None,qa_status=stage.get('qa_status'),qa_expected_text=stage.get('qa_expected_text'),model_sha256=stage.get('model_sha256'),wav_sha256=stage.get('wav_sha256'),reason_codes=stage.get('reason_codes',[]),cause=stage.get('error')) for stage in record.get('stt_stages',[])]
    cause=record.get('qa_deferral_reason') or record.get('error')
    if copy:return dict(status='not_required',transcript=None,model=None,stages=[],cause=None)
    if not valid:status='invalid'
    elif record.get('qa_completion_status')=='deferred_manual_review':status='deferred'
    elif record.get('stt_status') in ('fail','error','failed'):status='failed'
    elif record.get('stt_status') in ('pass','warn') and isinstance(record.get('transcript'),str):status='completed'
    else:status='absent'
    return dict(status=status,transcript=record.get('transcript') if status=='completed' else None,model=record.get('stt_model'),stages=stages,cause=cause,second_check_required=record.get('medium_checked') if isinstance(record.get('medium_checked'),bool) else None)
