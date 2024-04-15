import sys
import re

def snake_case(name):
    return name.lower().replace(" ", "_")

def update_links(section, content, sections):
    updated_content = []
    for line in content:
        matches = re.findall(r'\[([^\]]+)\]\(#([^\)]+)\)', line)
        for text, anchor in matches:
            # Check if this link points to a section within the same file
            if any(s for s in sections if anchor.replace('-', ' ') == s.lower()):
                target_section = next(s for s in sections if anchor.replace('-', ' ') == s.lower())
                if target_section.lower() == section.lower():
                    # Link within the same file, no change
                    continue
                else:
                    # Link to another section/file, update link
                    new_link = f'[{text}](./{snake_case(target_section)}.md#{anchor})'
                    line = line.replace(f'[{text}](#{anchor})', new_link)
        updated_content.append(line)
    return updated_content

def process_markdown(file_path):
    with open(file_path, encoding="utf-8") as file:
        lines = file.readlines()

    sections = {}
    current_section = "index"
    sections[current_section] = []

    for line in lines:
        if line.startswith("##") and line.count("#") == 2:
            section_title = line.strip("# \n")
            current_section = section_title
            sections[current_section] = []
        else:
            sections[current_section].append(line)

    section_titles = list(sections.keys())

    for section, content in sections.items():
        if section != "index":  # Skip creating a file for the index itself
            content = update_links(section, content, sections)
            file_name = f"{snake_case(section)}.md"
            with open(file_name, "w", encoding="utf-8") as file:
                file.write(f"# {section}\n{''.join(content).replace('## ', '# ').strip()}")

    # Optionally, modify the index file to remove all sections and keep only the links
    with open(file_path, "w", encoding="utf-8") as file:
        for section in section_titles:
            if section != "index":
                file_name = snake_case(section)
                file.write(f'- "{section}": "docs/concepts/{file_name}.md"\n')

if __name__ == "__main__":
    process_markdown(sys.argv[1])
