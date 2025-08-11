import json
import os
import random
import tqdm


def load_jsonl_file(path, n_data=None, is_random_sample=False):
    print(f"Loading File {path}")
    if not os.path.exists(path):
        print(f"{path} does not exist")
        return []
    with open(path, "r", encoding="utf-8") as fin:
        if n_data is None:
            return [json.loads(line) for line in fin.readlines()]

        if not is_random_sample:
            objs = []
            for i, line in tqdm.tqdm(enumerate(fin)):
                if i >= n_data:
                    break
                objs.append(json.loads(line))
            return objs
        else:
            return random.sample([json.loads(line) for line in fin.readlines()], n_data)


def write_jsonl(data, path):
    with open(path, "w", encoding="utf-8") as f:
        for d in data:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
