from importlib.metadata import version


def main(argv):
    del argv
    print(version("vpn2"))
