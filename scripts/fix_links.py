from pathlib import Path
import re
import sys


def transform_string(input_string):
    # Regex to match the pattern and capture the section after #
    pattern = r'\./(concepts\.md)#([a-z0-9-]+)'

    # Replacement function to transform captured groups
    def replacement(match):
        filename, section = match.groups()
        # Replace .md with / and dashes with underscores in the section part
        return f"./{filename[:-3]}/{section.replace('-', '_')}.md"

    # Apply the regex substitution
    return re.sub(pattern, replacement, input_string)


Path(sys.argv[1]).write_text(transform_string(Path(sys.argv[1]).read_text()))
