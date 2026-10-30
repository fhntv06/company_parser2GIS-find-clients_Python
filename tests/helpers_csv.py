import csv
import tempfile


def write_csv(rows, header=("id", "name", "address", "phones", "websites", "category")):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig", newline="")
    w = csv.writer(f); w.writerow(header); w.writerows(rows); f.close()
    return f.name
