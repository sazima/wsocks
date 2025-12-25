import setuptools
version = '1.0.4'

with open("README.md", "r", encoding="utf8") as f:
    readme = f.read()

setuptools.setup(
    name='wsocks',
    version=version,

    # 自动查找所有包
    packages=setuptools.find_packages(),

    # 包含所有包数据
    include_package_data=True,

    # 元数据
    long_description=readme,
    long_description_content_type="text/markdown",
    url='https://github.com/sazima/WSocks',

    # 入口点
    entry_points={
        'console_scripts': [
            'wsocks_client=wsocks.run_client:main',
            'wsocks_server=wsocks.run_server:main',
        ],
    },

    # 依赖
    install_requires=[
        'tornado',
        'websockets',
        'xxhash>=3.0.0',
        'msgpack',
        'uvloop; sys_platform != "win32"',  # Linux/macOS 默认安装高性能事件循环
    ],

    # 可选依赖（用于特殊场景）
    extras_require={
        'no-uvloop': [],  # 如果不想安装 uvloop: pip install wsocks[no-uvloop]
    },

    # 使用 wheel 格式而不是 egg
    zip_safe=False,

    # Python 版本要求
    python_requires='>=3.6',
)
