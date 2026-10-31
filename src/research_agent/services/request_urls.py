"""Extract user-provided URLs without swallowing surrounding prose punctuation."""

import re


def request_urls(text):
    results = []
    for match in re.finditer(r'https?://[^\s<>"`，。；！？、]+', text):
        token = match.group()
        stack = []
        closing = {")": "(", "]": "[", "}": "{", "）": "（"}
        for i, char in enumerate(token):
            if char in "([{（":
                stack.append(char)
            elif char in closing:
                if not stack or stack[-1] != closing[char]:
                    token = token[:i]
                    break
                stack.pop()
        token = token.rstrip(".,;:!?'\"")
        if token:
            results.append(token)
    return list(dict.fromkeys(results))
