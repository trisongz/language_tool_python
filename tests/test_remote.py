
import language_tool_python

tool = language_tool_python.LanguageTool('en-US', remote_server = "https://languagetool.v1.gexapi.com/")

s = "Department of medicine Colombia University closed on August 1 Milinda Samuelli"
s = "This work is subject to copyright. All rights are reserved by the Publisher, whether the whole or part of the material is concerned, specifically the rights of translation, reprinting, reuse of illustrations, recita-tion, broadcasting, reproduction on microfilms or in any other physical way, and transmission or infor-mation storage and retrieval, electronic adaptation, computer software, or by similar or dissimilar meth-odology now known or hereafter developed."
is_bad_rule = lambda rule: rule.message == 'Possible spelling mistake found.' and len(rule.replacements) and rule.replacements[0][0].isupper()

matches = tool.check(s)
matches = [rule for rule in matches if not is_bad_rule(rule)]
print(language_tool_python.utils.correct(s, matches))