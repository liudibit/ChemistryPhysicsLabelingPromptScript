#!/usr/bin/env python3
"""化学复合题大题级打标入口。
示例：python generate_chemistry_compound_tags.py --input "1015题目/化学200题-1015.xlsx" --limit 150
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tag_compound_science import main

if __name__ == '__main__':
    sys.argv.insert(1, 'chemistry')
    main()
