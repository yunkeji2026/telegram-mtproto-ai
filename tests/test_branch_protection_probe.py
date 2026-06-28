"""临时探测：故意失败用例，验证分支保护能否挡住红 CI 的合并。

本文件用完即删（连同分支/ PR）。它存在的唯一目的是让 `regression` 这个
必需检查变红，从而确认 main 的 branch protection 真正拦截合并。
"""


def test_branch_protection_should_block_merge():
    assert False, "intentional failure to verify branch protection blocks merge"
