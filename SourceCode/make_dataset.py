import copy
import pickle
import random
import re
import time
from typing import Dict, List

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

from utils.load_save_file import load_jsonl_file, write_jsonl
from utils.question_templates import pid_to_question_template

# Network Settings
session = requests.Session()
retries = Retry(total=5, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
})
session.proxies.update({
    "http": "http://127.0.0.1:7890",
    "https": "http://127.0.0.1:7890"
})

base_url = "https://www.wikidata.org/w/api.php"


def _get_qid(entity_name_list: List[str]) -> Dict[str, str]:
    """
    Fetch QID from WikiData by entity name
    Note: When the corresponding QID fails to be fetched, the entry is skipped.
    """
    entity_name_2_qid = {}
    for entity_name in tqdm(entity_name_list):
        params = {
            "action": "wbsearchentities",
            "format": "json",
            "search": entity_name,
            "language": "en",
            "type": "item",
            "limit": 1,
            "props": "url",
            "formatversion": 2,
        }
        try:
            response = session.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json().get("search")[0]
            qid = data["id"]
            entity_name_2_qid[entity_name] = qid
        except requests.exceptions.RequestException as e1:
            print(f"[Network Error] Failed to fetch {entity_name}: {e1}")
        except ValueError as e2:
            print(f"[Parse Error] Invalid JSON response for {entity_name}: {e2}")
        except KeyError as e3:
            print(f"[Key Error] Invalid Response, Probably fail to match for {entity_name}: {e3}")

        time.sleep(random.uniform(0.2, 0.5))

    return entity_name_2_qid


def _get_wikidata_statements(QID_List: List[str]) -> Dict[str, Dict]:
    """
    Fetch all statements recorded in WikiData by QID
    Note: When the corresponding statement fails to be fetched, the entry is skipped.
    """
    qid_2_statements = {}
    batch_size = 50

    for i in tqdm(range(0, len(QID_List), batch_size)):
        batch_qids = QID_List[i:i + batch_size]
        ids_param = "|".join(batch_qids)
        params = {
            "action": "wbgetentities",
            "format": "json",
            "ids": ids_param,
            "props": "claims",
            "languages": "en",
            "formatversion": 2
        }

        try:
            response = session.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            entities = data.get("entities", {})
            for qid, info in entities.items():
                claims = info.get("claims", {})
                if claims:
                    qid_2_statements[qid] = claims
        except requests.exceptions.RequestException as e:
            print(f"[Network Error] Failed to fetch batch {batch_qids}: {e}")
        except ValueError:
            print(f"[Parse Error] Invalid JSON response for batch {batch_qids}")

        time.sleep(random.uniform(0.2, 0.5))

    return qid_2_statements


def _extract_triplets(qid_statement_dict: Dict) -> List:
    """
    analysis obtained statements and extract into entity-relation-entity triplets
    """

    def process_wikidata_time(value):
        if value.startswith('+'):
            value = value[1:]
        value = value.replace('Z', '')

        year = value[0:4]
        month = value[5:7]
        day = value[8:10]

        results = [year]

        try:
            month_int = int(month)
            if 1 <= month_int <= 12:
                results.append(f"{year}-{month}")
                day_int = int(day)
                if 1 <= day_int <= 31:
                    results.append(f"{year}-{month}-{day}")
        except:
            pass

        return results

    triplets = []
    for qid, claims in qid_statement_dict.items():
        for pid, contents in claims.items():
            if pid not in pid_to_question_template.keys():  # skip if the relation is not selected
                continue
            acceptable_answers = []
            for ctx in contents:
                data_type = ctx["mainsnak"]["datatype"]
                if ctx["mainsnak"]["snaktype"] == "somevalue" or ctx["mainsnak"]["snaktype"] == "novalue":
                    continue
                if data_type == "time":
                    value = ctx["mainsnak"]["datavalue"]["value"]["time"]
                    acceptable_answers += process_wikidata_time(value)
                else:
                    acceptable_answers += [ctx["mainsnak"]["datavalue"]["value"]["id"]]
            triplets.append([qid, pid, acceptable_answers])
    return triplets


def _get_entity_name_from_qid(QID_List):
    """
        Fetch entity name from WikiData by QID
        Note: When fail to fetch, the entry is skipped.
    """
    qid_2_entity_name = {}
    batch_size = 50

    for i in tqdm(range(0, len(QID_List), batch_size)):
        batch_qids = QID_List[i:i + batch_size]
        ids_param = "|".join(batch_qids)
        params = {
            "action": "wbgetentities",
            "format": "json",
            "ids": ids_param,
            "props": "labels",
            "languages": "en",
            "formatversion": 2
        }
        try:
            response = session.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            entities = data.get("entities", {})
            for qid, info in entities.items():
                try:
                    title = info.get("labels").get("en").get("value")
                    qid_2_entity_name[qid] = title
                except AttributeError:
                    pass
        except requests.exceptions.RequestException as e:
            print(f"[Network Error] Failed to fetch batch {batch_qids}: {e}")
        except ValueError:
            print(f"[Parse Error] Invalid JSON response for batch {batch_qids}")
        time.sleep(random.uniform(0.2, 0.5))
    return qid_2_entity_name


def _organize_dataset(triplets, qid_2_entity_name):
    data = []
    for triplet in triplets:
        qid, pid, answers = triplet
        if qid_2_entity_name[qid] is None:
            print(f"Entity Name Not Found: {qid}")
            continue
        answers = [qid_2_entity_name[ans] if ans.startswith("Q") else ans for ans in answers]
        d = {
            "qid": qid,
            "question_list": [template.replace("${entity}", qid_2_entity_name[qid]) for template in
                              pid_to_question_template[pid]],
            "question_entity": qid_2_entity_name[qid],
            "relation": pid,
            "answer_list": answers
        }
        data.append(d)
    return data


def make_dataset(
        data_path="data/granola_eq/origin.jsonl",  # fill-in the original data path
        output_path="data/granola_eq/augmented.jsonl",  # fill-in the output path
):
    data = load_jsonl_file(data_path)

    # Step 1: Fetch the QID of each entity from the original dataset
    entity_name_list = [d["question_entity"] for d in data]
    entity_name_list = list(set(entity_name_list))
    entity_name_2_qid = _get_qid(entity_name_list)
    qid_list = list(entity_name_2_qid.values())

    # Step 2: Collect the statements for each entity in Wikidata
    qid_statement_dict = _get_wikidata_statements(qid_list)

    # Step 3: Extract triplets from the statements
    triplets = _extract_triplets(qid_statement_dict)
    # Fetch entity name of connected entities
    qids = []
    for pair in triplets:
        qids.append(pair[0])
        for answer in pair[2]:
            if answer.startswith("Q"):
                qids.append(answer)
    qids = list(set(qids))
    qid_to_entity_name = _get_entity_name_from_qid(qids)

    # Step 4. Organize Dataset
    data = _organize_dataset(triplets, qid_to_entity_name)
    write_jsonl(data, output_path)


def _is_time_string(s: str) -> bool:
    """
    Evaluate whether the input string is a valid date format:
    - YYYY
    - YYYY-MM
    - YYYY-MM-DD
    """
    if not isinstance(s, str):
        return False

    patterns = [
        r"^\d{4}$",  # YYYY
        r"^\d{4}-(0[1-9]|1[0-2])$",  # YYYY-MM
        r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",  # YYYY-MM-DD
    ]

    return any(re.match(p, s) for p in patterns)


def _get_alias_name_from_entity_name_list(entity_name_list: List[str]) -> Dict:
    base_url = "https://www.wikidata.org/w/api.php"
    entity_name_to_aliases_dict = {}

    batch_size = 50
    total = len(entity_name_list)

    for i in tqdm(range(0, total, batch_size)):
        batch_titles = entity_name_list[i:i + batch_size]
        titles = "|".join(batch_titles)

        params = {
            "action": "wbgetentities",
            "format": "json",
            "sites": "enwiki",
            "titles": titles,
            "props": "aliases|labels",
            "languages": "en",
            "formatversion": 2
        }

        try:
            response = session.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            entities = data.get("entities", {})
            for qid, info in entities.items():
                try:
                    entity_name = info.get("labels")["en"]["value"]
                    aliases = info.get("aliases")["en"]
                    aliases = [a["value"] for a in aliases]
                    entity_name_to_aliases_dict[entity_name] = aliases
                except Exception as e:
                    pass

        except requests.exceptions.RequestException as e:
            print(f"[Network Error] Failed to fetch batch {batch_titles}: {e}")
        except ValueError:
            print(f"[Parse Error] Invalid JSON response for batch {batch_titles}")

        # 防止频繁请求，随机延迟
        time.sleep(random.uniform(0.2, 0.5))
    return entity_name_to_aliases_dict


def find_alias_of_answers(
        data_path, output_path
):
    data = load_jsonl_file(data_path)
    answers = []
    for d in data:
        for ans in d["answer_list"]:
            if _is_time_string(ans):
                continue
            answers.append(ans)
    answers = list(set(answers))
    entity_name_to_aliases_dict = _get_alias_name_from_entity_name_list(answers)
    for d in data:
        answer_list_with_alias = []
        for ans in d["answer_list"]:
            try:
                aliases = entity_name_to_aliases_dict[ans]
                answer_list_with_alias += aliases
            except KeyError:
                pass
            answer_list_with_alias.append(ans)
        d["answer_list"] = answer_list_with_alias
    write_jsonl(data, output_path)


def unzip_to_single_questions(
        data_path, output_path
):
    data = load_jsonl_file(data_path)
    single_question_data = []
    for d in tqdm(data):
        q_list = d.pop("question_list")
        for q in q_list:
            w = copy.deepcopy(d)
            w["question"] = q
            single_question_data.append(w)
    write_jsonl(single_question_data, output_path)


def filter_dataset(
    data_path, output_path,
):
    """
    In rare cases, some questions may have no answer; these entries should be filtered out
    """
    data = load_jsonl_file(data_path)
    for d in data:
        ans_list = []
        for ans in d["answer_list"]:
            if ans is None:
                print(d)
                continue
            ans_list.append(ans)
        d["answer_list"] = ans_list
    write_jsonl(data, output_path)

    data = load_jsonl_file(output_path)
    filtered_data = []
    cnt = 0
    for d in data:
        if d["answer_list"] is None or len(d["answer_list"]) == 0:
            # print(d)
            cnt += 1
            continue
        try:
            a = int(d["answer_list"][0])
            if a < 0:
                # print(d)
                cnt += 1
                continue
        except:
            pass
        filtered_data.append(d)
    write_jsonl(filtered_data, output_path)
    print(cnt)


if __name__ == '__main__':
    data_path = "data/granola_eq/origin.jsonl"  # fill-in the original data path
    output_path = "data/granola_eq/augmented.jsonl"  # fill-in the output path
    make_dataset(data_path, output_path)
    find_alias_of_answers(output_path, output_path)
    unzip_to_single_questions(output_path, output_path)
    filter_dataset(output_path, output_path)
