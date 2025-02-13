import openai
from azure.search.documents import SearchClient
from azure.search.documents.models import QueryType
from approaches.approach import Approach
from text import nonewlines

# Simple retrieve-then-read implementation, using the Cognitive Search and OpenAI APIs directly. It first retrieves
# top documents from search, then constructs a prompt with them, and then uses OpenAI to generate an completion 
# (answer) with that prompt.
class ChatReadRetrieveReadApproach(Approach):
    prompt_prefix = """<|im_start|>system
Vorbesti MEREU in limba romana.NU este permis raspunsul in engleza sau alte limbi, DOAR in romaneste.
Esti asistentul inteligent al medicului de neonatologie si al rezidentilor care incep acum. Scopul tau este sa fi cat mai de folos, sa ii indrumi cat mai concret si corect. Te vei limita la documentatia pe care o ai la indemana, insa nu vei prioritiza niciunul din documente. 
MEREU daca informatia SE REGASESTE, IN MAI MULTE SURSE, le vei puncta pe fiecare in parte, de fiecare data.
Informatia se regaseste si in alte pdf-uri deci TREBUIE sa le expui sursa pentru fiecare caz.
Te chinui sa sintetizezi informatia cat mai clar si concret. 
DACA exista proceduri, faci lista cu pasi, a,b,c,d.
Important de stiut ca vei ajuta medicul sa ia decizii, astfel incat atunci cand nu esti sigur, spui ca nu esti sigur si indrumi medicul catre niste surse pentru a verifica informatia.
 Rămâi concentrat pe subiect și evită să te abati de la subiectul conversatiei.
Vei fi capabil să găsești soluții pentru problemele întâlnite, dar MEREU vei ține cont de datele și informațiile pe care ai fost antrenat si te vei raporta la ele.
Raspunsul incepe mereu cu "Conform documentului <pdf-ul> si raspunsul. DACA exista si in alt document, spui 'Am mai gasit si in documentul <pdf-ul 2> si mesajul'.

Pentru informații tabulare, returneaza-le sub forma unei tabele HTML. NU returna formatul Markdown.
FIECARE sursă are un nume urmat de doua-puncte și informația actuală; include întotdeauna numele sursei pentru fiecare fapt pe care îl utilizezi în răspuns. Utilizeaza paranteze pătrate pentru a face referire la sursă, de exemplu [info1.txt]. Nu combina sursele, listeaza fiecare sursă separat, de exemplu [info1.txt][info2.pdf].

{follow_up_questions_prompt}
{injected_prompt}
Surse:
{sources}
<|im_end|>
{chat_history}
"""

    follow_up_questions_prompt_content = """Generează trei întrebări foarte scurte de continuare pe care utilizatorul le-ar pune probabil despre competenta, experienta sau detalii ale persoanei.
    Il ajuti pe user sa inteleaga mai bine. 
    Utilizeaza ghilimele duble unghiulare pentru a face referire la întrebări, de exemplu <<Doresti mai multe detalii?>>. 
    Încearca să nu repeți întrebările care au fost deja puse. 
    Genereaza doar întrebări și nu genera niciun text înainte sau după întrebări, cum ar fi "Următoarele întrebări".'"""

    query_prompt_template = """Mai jos se află istoricul conversației până acum și o nouă întrebare adresată de utilizator care trebuie răspunsă prin căutarea într-o bază de cunoștințe pe care o ai legata de documentele pe care ai fost antrenat.
Genereaza o interogare de căutare bazată pe conversația anterioară și pe noua întrebare. 
Nu include numele fișierelor sau numele documentelor citate, de exemplu info.txt sau doc.pdf, în termenii interogării de căutare. 
Nu include niciun text în interiorul parantezelor pătrate [] sau ghilimelelor duble unghiulare <<>> în termenii interogării de căutare.
Dacă întrebarea nu este în limba romana, traduci întrebarea în limba romana înainte de a genera interogarea de căutare.

Istoric Chat:
{chat_history}

Intrebare:
{question}

Cautare query:
"""

    def __init__(self, search_client: SearchClient, chatgpt_deployment: str, gpt_deployment: str, sourcepage_field: str, content_field: str):
        self.search_client = search_client
        self.chatgpt_deployment = chatgpt_deployment
        self.gpt_deployment = gpt_deployment
        self.sourcepage_field = sourcepage_field
        self.content_field = content_field

    def run(self, history: list[dict], overrides: dict) -> any:
        use_semantic_captions = True if overrides.get("semantic_captions") else False
        top = overrides.get("top") or 3
        exclude_category = overrides.get("exclude_category") or None
        filter = "category ne '{}'".format(exclude_category.replace("'", "''")) if exclude_category else None

        # STEP 1: Generate an optimized keyword search query based on the chat history and the last question
        prompt = self.query_prompt_template.format(chat_history=self.get_chat_history_as_text(history, include_last_turn=False), question=history[-1]["user"])
        completion = openai.Completion.create(
            engine=self.gpt_deployment, 
            prompt=prompt, 
            temperature=0.0, 
            max_tokens=32, 
            n=1, 
            stop=["\n"])
        q = completion.choices[0].text

        # STEP 2: Retrieve relevant documents from the search index with the GPT optimized query
        if overrides.get("semantic_ranker"):
            r = self.search_client.search(q, 
                                          filter=filter,
                                          query_type=QueryType.SEMANTIC, 
                                          query_language="en-us", 
                                          query_speller="lexicon", 
                                          semantic_configuration_name="default", 
                                          top=top, 
                                          query_caption="extractive|highlight-false" if use_semantic_captions else None)
        else:
            r = self.search_client.search(q, filter=filter, top=top)
        if use_semantic_captions:
            results = [doc[self.sourcepage_field] + ": " + nonewlines(" . ".join([c.text for c in doc['@search.captions']])) for doc in r]
        else:
            results = [doc[self.sourcepage_field] + ": " + nonewlines(doc[self.content_field]) for doc in r]
        content = "\n".join(results)

        follow_up_questions_prompt = self.follow_up_questions_prompt_content if overrides.get("suggest_followup_questions") else ""
        
        # Allow client to replace the entire prompt, or to inject into the exiting prompt using >>>
        prompt_override = overrides.get("prompt_template")
        if prompt_override is None:
            prompt = self.prompt_prefix.format(injected_prompt="", sources=content, chat_history=self.get_chat_history_as_text(history), follow_up_questions_prompt=follow_up_questions_prompt)
        elif prompt_override.startswith(">>>"):
            prompt = self.prompt_prefix.format(injected_prompt=prompt_override[3:] + "\n", sources=content, chat_history=self.get_chat_history_as_text(history), follow_up_questions_prompt=follow_up_questions_prompt)
        else:
            prompt = prompt_override.format(sources=content, chat_history=self.get_chat_history_as_text(history), follow_up_questions_prompt=follow_up_questions_prompt)

        # STEP 3: Generate a contextual and content specific answer using the search results and chat history
        completion = openai.Completion.create(
            engine=self.chatgpt_deployment, 
            prompt=prompt, 
            temperature=overrides.get("temperature") or 0.7, 
            max_tokens=1024, 
            n=1, 
            stop=["<|im_end|>", "<|im_start|>"])

        return {"data_points": results, "answer": completion.choices[0].text, "thoughts": f"Searched for:<br>{q}<br><br>Prompt:<br>" + prompt.replace('\n', '<br>')}
    
    def get_chat_history_as_text(self, history, include_last_turn=True, approx_max_tokens=1000) -> str:
        history_text = ""
        for h in reversed(history if include_last_turn else history[:-1]):
            history_text = """<|im_start|>user""" +"\n" + h["user"] + "\n" + """<|im_end|>""" + "\n" + """<|im_start|>assistant""" + "\n" + (h.get("bot") + """<|im_end|>""" if h.get("bot") else "") + "\n" + history_text
            if len(history_text) > approx_max_tokens*4:
                break    
        return history_text