"""Behaviour checks for the speech -> edit step against the real model.

Each case is a buffer with a caret (written as |) or selection (written as
<<...>>), a spoken instruction, and a predicate on the resulting buffer.
These call a real model, so they are run on demand:
    python tests/model_cases.py                      # your configured provider
    python tests/model_cases.py PROVIDER [MODEL]     # e.g. openrouter some/model
    python tests/model_cases.py --only rename        # a single case
"""
import sys, re
from concurrent.futures import ThreadPoolExecutor
from vocaret import config, edits as E
from vocaret.engine import Engine

CASES = [
 ("py method in class", "python", "4 spaces",
  "class Rect:\n    def __init__(self, width, height):\n        self.width = width\n        self.height = height\n\n    |\n",
  "add a method called area that returns width times height",
  lambda t: "\n    def area(self):\n        return self.width * self.height\n" in t and t.count("def ") == 2),
 ("java caret after code needs new line", "java", "4 spaces",
  "void run() {\n    int x = 5;|\n}\n",
  "then declare a string called name set to hello",
  lambda t: '    int x = 5;\n    String name = "hello";\n}' in t),
 ("inline completion mid-line", "python", "4 spaces",
  "def checkout(items, tax_rate):\n    result = |\n    return result\n",
  "call compute total with items and tax rate",
  lambda t: "    result = compute_total(items, tax_rate)\n    return result\n" in t),
 ("rename across file", "javascript", "2 spaces",
  "function tally(list) {\n  let x = 0;\n  for (const item of list) {\n    x += item;\n  }|\n  return x;\n}\n",
  "rename x to count",
  lambda t: "let count = 0" in t and "count += item" in t and "return count" in t and not re.search(r"\bx\b", t)),
 ("not an instruction", "python", "4 spaces",
  "import sys\n|\n",
  "hey can you grab me a coffee on your way back thanks",
  lambda t: t == "import sys\n\n"),
 ("self-correction", "javascript", "2 spaces",
  "|",
  "make a function called foo, no wait, call it bar, and it takes nothing and returns forty two",
  lambda t: "function bar()" in t and "return 42" in t and "foo" not in t),
 ("selection wrap", "java", "4 spaces",
  "void load() {\n    <<data = reader.read();>>\n}\n",
  "wrap this in a try catch that catches IO exception and prints the stack trace",
  lambda t: "try {" in t and "        data = reader.read();" in t and "catch (IOException e)" in t and "e.printStackTrace();" in t),
 ("delete member", "java", "4 spaces",
  "public class Foo {\n    public Foo(String s) {\n        System.out.println(s);\n    }\n\n    public String test(String x, String y) {\n        return x + y;\n    }|\n}\n",
  "delete the constructor",
  lambda t: "Foo(String s)" not in t and "public String test(String x, String y) {\n        return x + y;\n    }" in t and t.startswith("public class Foo {\n    public String test")),
 ("go with tabs", "go", "tabs",
  "package main\n\n|\n",
  "add a function add that takes two ints a and b and returns their sum",
  lambda t: "func add(a, b int) int {\n\treturn a + b\n}" in t or "func add(a int, b int) int {\n\treturn a + b\n}" in t),
 ("misheard identifier", "typescript", "2 spaces",
  "function next(userId: number): number {\n  |\n}\n",
  "return user idea plus one",
  lambda t: "  return userId + 1;\n" in t),
 ("nothing extra", "java", "4 spaces",
  "public class User {\n    |\n}\n",
  "add a private field called name of type string",
  lambda t: t == "public class User {\n    private String name;\n}\n"),
 ("caret before existing code", "python", "4 spaces",
  "def main():\n    |print('done')\n",
  "set total to zero",
  lambda t: t == "def main():\n    total = 0\n    print('done')\n"),
 ("cpp loop described by behaviour", "cpp", "4 spaces",
  "#include <iostream>\n#include <vector>\n\nvoid show(const std::vector<int>& items) {\n    |\n}\n",
  "loop over the items and print each one on its own line",
  lambda t: "for (" in t and "std::cout <<" in t and t.count("#include") == 2),
]

def run(case, cfg):
    name, lang, indent, buf, spoken, check = case
    m = re.search(r"<<(.*?)>>", buf, re.S)
    if m:
        text = buf[:m.start()] + m.group(1) + buf[m.end():]
        a, b = m.start(), m.start() + len(m.group(1))
    else:
        a = b = buf.index("|"); text = buf.replace("|", "", 1)
    pm = E.PositionMap(text)
    try:
        res = Engine().instruct(spoken, {"path": "work." + lang, "language": lang, "indent": indent,
              "text": text, "selection": {"start": pm.to_position(a), "end": pm.to_position(b)}}, cfg)
        new = E.apply_edits(text, [(pm.to_index(e["start"]), pm.to_index(e["end"]), e["text"]) for e in res["edits"]])
        return name, check(new), new, res
    except Exception as exc:
        return name, False, f"EXCEPTION {exc}", {}

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else ""
    if only in args:
        args.remove(only)
    cfg = config.resolve({"provider": args[0] if args else None,
                          "model": args[1] if len(args) > 1 else None})
    cases = [c for c in CASES if only in c[0]]
    with ThreadPoolExecutor(5) as pool:
        results = list(pool.map(lambda c: run(c, cfg), cases))
    bad = 0
    for name, ok, new, res in results:
        print(("PASS " if ok else "FAIL ") + name + f"  [{res.get('llm_ms', '?')} ms]" + (f"  note: {res['note']}" if res.get("note") else ""))
        if not ok:
            bad += 1
            print("----- result\n" + new + "\n----- reply\n" + str(res.get("reply")) + "\n-----")
    print(f"{len(results) - bad}/{len(results)} passed with {cfg['provider']}"
          + (f" / {cfg['model']}" if cfg["model"] else ""))
    sys.exit(1 if bad else 0)
