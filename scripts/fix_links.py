from pathlib import Path
import re
import sys


def transform_string(input_string):
    pattern = r'\./(concepts\.md)#([a-z0-9-]+)'

    def replacement(match):
        filename, section = match.groups()
        return f"./{filename[:-3]}/{section.replace('-', '_')}.md"

    return re.sub(pattern, replacement, input_string)


Path(sys.argv[1]).write_text(transform_string(Path(sys.argv[1]).read_text()))
