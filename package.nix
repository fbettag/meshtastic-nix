{ lib, python3Packages }:
let
  # Upstream's optional test extras include Linux-only TUN and lab hardware.
  # Use its core TCP library on both platforms; test the reconciler below.
  meshtasticTCP = python3Packages.meshtastic.overridePythonAttrs {
    nativeCheckInputs = [ ];
    doCheck = false;
    preCheck = "";
  };
in
python3Packages.buildPythonApplication {
  pname = "meshtastic-nix";
  version = "0.1.1";
  pyproject = true;
  src = lib.cleanSource ./.;
  build-system = [ python3Packages.setuptools ];
  dependencies = [
    meshtasticTCP
    python3Packages.protobuf
  ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  pythonImportsCheck = [ "meshtastic_nix" ];
  meta = {
    description = "Declarative Meshtastic configuration over TCP";
    license = lib.licenses.gpl3Only;
    mainProgram = "meshtastic-nix";
    platforms = lib.platforms.unix;
  };
}
