'''
根据diff里面的变更行，到真实文件当中找上下文
比如说120行变了，找到前后20行，100-140
然后把上下文挂到 changed_file.hunks.context_lines 里面

后面可以实现函数边界和class边界，使用tree-sitter来解析python文件，找到函数和类的边界，然后把上下文挂到 changed_file.hunks.context_lines 里面
'''