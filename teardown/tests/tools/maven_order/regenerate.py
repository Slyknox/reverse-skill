"""Regenerate tests/fixtures/maven_order.json from Maven's own ComparableVersion.

Needs a JDK and network access. Not run by the test suite; the fixture it writes is.

    python3 tests/tools/maven_order/regenerate.py

It downloads maven-artifact 3.9.9 from Maven Central (checked against its .sha1),
compiles ComparePairs.java, builds a corpus (Maven's own test lists, generated
tricky versions, and real versions from deps.dev), asks Java for the sign of
compareTo for sampled pairs, and stores them.
"""

import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "fixtures", "maven_order.json")
JAR_URL = "https://repo1.maven.org/maven2/org/apache/maven/maven-artifact/3.9.9/maven-artifact-3.9.9.jar"
QUALIFIER_LIST = ["1-alpha2snapshot", "1-alpha2", "1-alpha-123", "1-beta-2", "1-beta123", "1-m2", "1-m11", "1-rc", "1-cr2",
	"1-rc123", "1-SNAPSHOT", "1", "1-sp", "1-sp2", "1-sp123", "1-abc", "1-def", "1-pom-1", "1-1-snapshot", "1-1", "1-2", "1-123"]
NUMBER_LIST = ["2.0", "2.0.a", "2-1", "2.0.2", "2.0.123", "2.1.0", "2.1-a", "2.1b", "2.1-c", "2.1-1", "2.1.0.1", "2.2",
	"2.123", "11.a2", "11.a11", "11.b2", "11.b11", "11.m2", "11.m11", "11", "11.a", "11b", "11c", "11m"]


def get(url: str) -> bytes:
	for attempt in range(6):
		try:
			with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "rea-teardown/0.6"}), timeout=30) as r:
				return r.read()
		except urllib.error.HTTPError as err:
			if err.code != 429 or attempt == 5:
				raise
			time.sleep(3 * (attempt + 1))


def corpus() -> list[str]:
	random.seed(20261102)
	parts = ["0", "1", "2", "3", "9", "10", "11", "01", "007", "123", "a", "b", "m", "x", "alpha", "beta", "milestone", "rc",
		"cr", "ga", "final", "release", "sp", "snapshot", "SNAPSHOT", "abc", "def", "pom", "RC", "Final", "jre", "android"]
	out = set(QUALIFIER_LIST + NUMBER_LIST)
	while len(out) < 450:
		v = random.choice(["0", "1", "2", "10"])
		for _ in range(random.randint(1, 5) - 1):
			v += random.choice([".", "-", ""]) + random.choice(parts)
		if random.random() < 0.1:
			v = "." + v
		if random.random() < 0.1:
			v += random.choice(["-", "."])
		out.add(v)
	for pkg in ["com.google.guava%3Aguava", "org.slf4j%3Aslf4j-api", "org.springframework%3Aspring-core",
			"com.fasterxml.jackson.core%3Ajackson-databind", "org.hibernate.orm%3Ahibernate-core", "io.netty%3Anetty-all"]:
		vs = [v["versionKey"]["version"] for v in json.loads(get(f"https://api.deps.dev/v3/systems/maven/packages/{pkg}"))["versions"]]
		random.shuffle(vs)
		out.update(vs[:60])
	return sorted(out)


def main() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		jar = os.path.join(tmp, "maven-artifact.jar")
		data = get(JAR_URL)
		if hashlib.sha1(data).hexdigest() != get(JAR_URL + ".sha1").decode().split()[0]:
			sys.exit("jar checksum mismatch")
		with open(jar, "wb") as fh:
			fh.write(data)
		subprocess.run(["javac", "-d", tmp, "-cp", jar, os.path.join(HERE, "ComparePairs.java")], check=True)
		versions = corpus()
		pairs = [(a, b) for i, a in enumerate(versions) for b in versions[i + 1:]]
		random.seed(7)
		sample = random.sample(pairs, 4000)
		out = subprocess.run(["java", "-cp", f"{jar}{os.pathsep}{tmp}", "ComparePairs"], input="".join(f"{a}\t{b}\n" for a, b in sample),
			capture_output=True, text=True, check=True).stdout.split()
	result = {"source": "org.apache.maven:maven-artifact:3.9.9 ComparableVersion.compareTo, sign per pair",
		"orders": {"qualifiers": QUALIFIER_LIST, "numbers": NUMBER_LIST},
		"pairs": [[a, b, int(s)] for (a, b), s in zip(sample, out)]}
	with open(OUT, "w", encoding="utf-8") as fh:
		json.dump(result, fh, indent=0, ensure_ascii=False)
		fh.write("\n")
	print(f"wrote {len(result['pairs'])} pairs to {OUT}")


if __name__ == "__main__":
	main()
