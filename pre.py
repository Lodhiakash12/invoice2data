from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

analyzer = AnalyzerEngine()
anonymizer = AnonymizerEngine()

with open("input.txt", "r", encoding="utf-8") as f:
    text = f.read()

results = analyzer.analyze(text=text, language="en")
anonymized = anonymizer.anonymize(text=text, analyzer_results=results)

with open("output.txt", "w", encoding="utf-8") as f:
    f.write(anonymized.text)