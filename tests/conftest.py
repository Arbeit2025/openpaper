import os
import sys

# プロジェクトルートを import パスに追加し、`import app` を可能にする
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
