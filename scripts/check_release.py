"""核对发布目录、插件版本与市场索引，避免市场显示错误版本。"""
import ast
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for flavor in ('v2', 'v3'):
    index = json.loads((root / f'package.{flavor}.json').read_text())
    for identifier, entry in index.items():
        source = root / f'plugins.{flavor}' / identifier.lower() / '__init__.py'
        tree = ast.parse(source.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == identifier)
        values = {target.id: ast.literal_eval(node.value)
                  for node in cls.body if isinstance(node, ast.Assign)
                  for target in node.targets if isinstance(target, ast.Name) and target.id in
                  {'plugin_version', 'plugin_author', 'plugin_icon', 'plugin_name'}}
        assert entry['version'] == values['plugin_version'] == next(iter(entry['history']))
        assert entry['author'] == values['plugin_author']
        assert entry['name'] == values['plugin_name']
        assert entry['icon'] == values['plugin_icon']
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'get_render_mode'
               for node in cls.body):
            assert (source.parent / 'frontend/dist/assets/remoteEntry.js').is_file()
        else:
            assert any(isinstance(node, ast.FunctionDef) and node.name == 'get_form' for node in cls.body)
        assert entry['release'] is False, 'Source installation uses main branch'
        print(flavor, identifier, entry['version'], 'metadata verified')
