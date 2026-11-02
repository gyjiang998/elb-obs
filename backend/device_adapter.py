"""Template-materialized adapters for autonomous device validation.

The browser model may decide *what* small, auditable adapter to try, but it
never gets an HDC command or an arbitrary host compiler invocation.  It emits a
strict data contract.  This module validates that contract, renders only a
supported template into the local artifact area, and invokes fixed compiler and
HDC argument vectors for the resulting immutable source file.

The first implementation deliberately supports two compact, real device paths:

* a GLES3 fragment expression over two RGBA pixels; and
* an NNRT FLOAT32 2-D MatMul graph with backend-computed CPU reference output.

DSP and media contracts can still be analysed, but no generic provider/component
adapter is claimed until an equally bounded runtime template is registered.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from .core import PlatformError, sha256_file, sha256_json
from .device_verification import (
    DEVICE_VALIDATION_TIMEOUT_SECONDS,
    _available_devices,
    _bounded,
    _clean_output,
    _run_hdc,
    _run_process,
    _safe_serial,
    resolve_hdc_binary,
)
from .platform_paths import llvm_bin_root, openharmony_sdk_root
from .device_experiment import LLD_THREAD_ARGUMENT


DEVICE_UNITS = frozenset({"npu", "gpu", "dsp", "media"})
ADAPTER_DECISIONS = frozenset({"ready_for_materialization", "needs_contract", "not_supported"})
GPU_ADAPTER_KIND = "gpu_gles3_rgba_expression_2px"
NPU_ADAPTER_KIND = "npu_nnrt_matmul_f32"
MAX_TEXT_LENGTH = 4_096
MAX_SOURCE_BYTES = 96 * 1024

GPU_BASE_SOURCE = Path("demo/hardware-api-examples/device-run/gpu_rgba_brightness_gles3_smoke.c")
NPU_BASE_SOURCE = Path("demo/hardware-api-examples/device-run/npu_nnrt_matmul_smoke.c")

GPU_REMOTE_PREFIX = "/data/local/tmp/hetero_gpu_adapter_"
NPU_REMOTE_PREFIX = "/data/local/tmp/hetero_npu_adapter_"

# The first GPU template deliberately has one verified CPU reference semantic.
# The model may select data and brightness, but it must not invent a reference
# result for an arbitrary shader expression.
GPU_BRIGHTNESS_EXPRESSION = "vec4(clamp(pixel.rgb+vec3(brightness),0.0,1.0),pixel.a)"


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _strings(value: Any, *, limit: int = 12, item_limit: int = 800) -> list[str]:
    if not isinstance(value, list):
        return []
    values = [_text(item, item_limit) for item in value]
    return [item for item in values if item][:limit]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _source_reference(code_artifact: dict[str, Any]) -> dict[str, Any]:
    source = _record(code_artifact.get("source"))
    return {
        "origin": _text(source.get("origin"), 120),
        "path": _text(source.get("path"), 600),
        "sha256": _text(source.get("sha256"), 80),
        "line_start": source.get("line_start") if isinstance(source.get("line_start"), int) else None,
        "line_end": source.get("line_end") if isinstance(source.get("line_end"), int) else None,
    }


def supported_adapter_catalog(unit: str) -> list[dict[str, Any]]:
    """Expose only templates that can be materialized by trusted code."""

    if unit == "gpu":
        return [{
            "adapter_kind": GPU_ADAPTER_KIND,
            "summary_zh": "固定 EGL/GLES3 host 中执行 RGBA 亮度叠加并裁剪；平台根据同一 CPU 语义计算两像素参考输出，再做 readback 比较。",
            "model_fields": ["fragment_expression", "brightness", "input_rgba"],
            "limits": ["当前只支持已登记的 pixel.rgb + brightness、clamp、保留 alpha 语义", "固定 2 个 RGBA 像素", "CPU 参考输出由平台计算，模型不得填写", "不能声明资源、循环、纹理访问或预处理指令"],
        }]
    if unit == "npu":
        return [{
            "adapter_kind": NPU_ADAPTER_KIND,
            "summary_zh": "以公开 NNRT 构建 FLOAT32 2-D MatMul，后端计算 CPU 参考值后在选定加速器上比较。",
            "model_fields": ["left_shape", "right_shape", "left_values", "right_values", "tolerance"],
            "limits": ["仅 MatMul", "shape 为最多 8x8 的二维 FLOAT32 张量", "CPU 参考输出由平台计算，不能由模型伪造"],
        }]
    return []


def build_device_adapter_context(
    *,
    code_artifact: dict[str, Any],
    dispatch_assessment_artifact: dict[str, Any],
    probe_artifact: dict[str, Any],
    requested_unit: str,
) -> dict[str, Any]:
    """Create the bounded context used for one adapter-synthesis model call."""

    unit = requested_unit.strip().casefold()
    if unit not in DEVICE_UNITS:
        raise ValueError("unsupported requested device unit")

    assessment = _record(dispatch_assessment_artifact.get("result"))
    validation = _record(assessment.get("validation"))
    source = _source_reference(code_artifact)
    source_sha = _text(source.get("sha256"), 80)
    allowed_evidence = {
        f"dispatch:{_text(dispatch_assessment_artifact.get('artifact_id'), 160)}",
        f"device_probe:{_text(probe_artifact.get('artifact_id'), 160)}",
        f"source:{source_sha}",
        *(_strings(assessment.get("evidence_ids"), limit=16, item_limit=240)),
    }
    allowed_evidence.discard("source:")
    allowed_evidence.discard("dispatch:")
    allowed_evidence.discard("device_probe:")
    observed_units = _record(probe_artifact.get("observed_units"))
    observation = _record(observed_units.get(unit))
    contract = _record(assessment.get("dispatch_contract"))
    model_input_context = {
        "schema_version": "device_adapter_model_input/v1",
        "task": "根据已经通过校验的语义下发合同，选择一个已登记的受控 adapter 模板并给出参数。不要生成 C、shell、HDC 或任意命令。",
        "requested_unit": unit,
        "source": source,
        "hotspot_code": _text(code_artifact.get("code"), 28 * 1024),
        "dispatch_assessment": {
            "decision": _text(assessment.get("decision"), 80),
            "target_unit": _text(assessment.get("target_unit"), 80),
            "execution_mode": _text(assessment.get("execution_mode"), 240),
            "summary": _text(assessment.get("summary")),
            "reasoning": _text(assessment.get("reasoning"), 6_000),
            "candidate_scope": _record(assessment.get("candidate_scope")),
            "observed_code_evidence": _strings(assessment.get("observed_code_evidence")),
            "dispatch_contract": {
                "semantic_operations": _strings(contract.get("semantic_operations")),
                "input_assumptions": _strings(contract.get("input_assumptions")),
                "output_semantics": _strings(contract.get("output_semantics")),
                "state_and_side_effects": _strings(contract.get("state_and_side_effects")),
                "required_adapter_work": _strings(contract.get("required_adapter_work")),
                "cpu_fallback": _strings(contract.get("cpu_fallback")),
                "result_comparison": _strings(contract.get("result_comparison")),
            },
            "validation_status": _text(validation.get("status"), 80),
            "validation_errors": _strings(validation.get("errors")),
        },
        "device_observation": {
            "unit_id": unit,
            "observation_status": _text(observation.get("observation_status"), 80) or "not_observed",
            "observed_capabilities": _strings(observation.get("observed_capabilities")),
            "claim_boundary_zh": _text(observation.get("claim_boundary_zh")),
        },
        "supported_templates": supported_adapter_catalog(unit),
        "constraints": [
            "只有 dispatch_assessment.decision=semantic_candidate 且 validation_status=valid 时，才可以返回 ready_for_materialization。",
            "GPU 模板只接受受限的 fragment_expression；NPU 模板只接受 FLOAT32 2-D MatMul 参数。",
            "平台会用可信模板生成 C 源码和固定编译/HDC 参数。模型不能提供完整 C、shell、HDC、驱动代码或任意构建命令。",
            "生成成功只验证该 adapter 的固定输入输出与设备路径，不证明完整热点、生产数据规模、性能或功耗收益。",
        ],
        "allowed_evidence_ids": sorted(allowed_evidence),
    }
    return {
        "schema_version": "device_adapter_context/v1",
        "code_artifact_id": _text(code_artifact.get("artifact_id"), 160),
        "dispatch_assessment_artifact_id": _text(dispatch_assessment_artifact.get("artifact_id"), 160),
        "probe_artifact_id": _text(probe_artifact.get("artifact_id"), 160),
        "requested_unit": unit,
        "model_input_context": model_input_context,
        "context_sha256": sha256_json(model_input_context),
    }


def _validate_scope(value: Any, errors: list[str]) -> dict[str, Any]:
    scope = _record(value)
    level = _text(scope.get("level"), 80)
    description = _text(scope.get("description"))
    if level not in {"function", "loop_region", "data_pipeline", "callee", "unknown"}:
        errors.append("source_scope.level 不受支持")
    if not description:
        errors.append("source_scope.description 不能为空")
    return {"level": level, "description": description}


def _validate_evidence(value: Any, allowed: set[str], errors: list[str]) -> list[str]:
    identifiers = _strings(value, limit=12, item_limit=240)
    if any(identifier not in allowed for identifier in identifiers):
        errors.append("evidence_ids 包含本轮上下文以外的证据")
    return identifiers


def _validate_rgba(value: Any, field: str, errors: list[str]) -> list[int]:
    if not isinstance(value, list) or len(value) != 8:
        errors.append(f"implementation.{field} 必须为 8 个 0-255 的 RGBA 字节")
        return []
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 255:
            errors.append(f"implementation.{field} 必须为 8 个 0-255 的 RGBA 字节")
            return []
        result.append(item)
    return result


_GPU_FORBIDDEN = re.compile(r"(?:#|;|\{|\}|\n|\r|\b(?:void|uniform|sampler|texture|layout|precision|for|while|do|discard)\b|\bgl_)", re.IGNORECASE)
_GPU_IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")
_GPU_IDENTIFIERS = frozenset({
    "pixel", "brightness", "r", "g", "b", "a", "rgb", "rgba", "x", "y", "z", "w",
    "vec2", "vec3", "vec4", "float", "clamp", "min", "max", "mix", "pow", "abs", "floor", "ceil",
    "step", "smoothstep", "sin", "cos", "fract", "mod", "dot", "normalize", "sqrt", "exp", "log",
})


def _validate_gpu_expression(value: Any, errors: list[str]) -> str:
    expression = _text(value, 2_400)
    if not expression:
        errors.append("implementation.fragment_expression 不能为空")
        return ""
    if _GPU_FORBIDDEN.search(expression):
        errors.append("implementation.fragment_expression 只能是受限 GLSL 表达式，不能声明资源、控制流或预处理指令")
        return ""
    identifiers = set(_GPU_IDENTIFIER.findall(expression))
    invalid = sorted(identifier for identifier in identifiers if identifier not in _GPU_IDENTIFIERS)
    if invalid:
        errors.append(f"implementation.fragment_expression 包含未允许的标识符：{', '.join(invalid[:4])}")
        return ""
    if "pixel" not in identifiers:
        errors.append("implementation.fragment_expression 必须显式使用 pixel")
    if expression.count("(") != expression.count(")") or expression.count("[") != expression.count("]"):
        errors.append("implementation.fragment_expression 括号不平衡")
    normalized = re.sub(r"\s+", "", expression)
    if normalized != GPU_BRIGHTNESS_EXPRESSION:
        errors.append("implementation.fragment_expression 当前模板只支持 vec4(clamp(pixel.rgb + vec3(brightness), 0.0, 1.0), pixel.a) 的已登记语义")
        return ""
    return GPU_BRIGHTNESS_EXPRESSION


def _gpu_brightness_reference(input_rgba: list[int], brightness: float) -> list[int]:
    """Calculate the registered CPU fallback for the GLES3 brightness template."""

    offset = brightness * 255.0
    expected: list[int] = []
    for index, channel in enumerate(input_rgba):
        if index % 4 == 3:
            expected.append(channel)
            continue
        bounded = min(255.0, max(0.0, channel + offset))
        expected.append(min(255, max(0, int(math.floor(bounded + 0.5)))))
    return expected


def _validate_gpu_implementation(value: Any, errors: list[str]) -> dict[str, Any]:
    implementation = _record(value)
    brightness = _finite_number(implementation.get("brightness"))
    if brightness is None or brightness < -1.0 or brightness > 1.0:
        errors.append("implementation.brightness 必须为 -1 到 1 的有限数")
    input_rgba = _validate_rgba(implementation.get("input_rgba"), "input_rgba", errors)
    return {
        "fragment_expression": _validate_gpu_expression(implementation.get("fragment_expression"), errors),
        "brightness": brightness if brightness is not None else 0.0,
        "input_rgba": input_rgba,
        "expected_rgba": _gpu_brightness_reference(input_rgba, brightness) if input_rgba and brightness is not None else [],
    }


def _shape(value: Any, field: str, errors: list[str]) -> list[int]:
    if not isinstance(value, list) or len(value) != 2:
        errors.append(f"implementation.{field} 必须是两个 1-8 的维度")
        return []
    dimensions: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1 or item > 8:
            errors.append(f"implementation.{field} 必须是两个 1-8 的维度")
            return []
        dimensions.append(item)
    return dimensions


def _float_values(value: Any, field: str, count: int, errors: list[str]) -> list[float]:
    if not isinstance(value, list) or len(value) != count:
        errors.append(f"implementation.{field} 长度必须为 {count}")
        return []
    result: list[float] = []
    for item in value:
        number = _finite_number(item)
        if number is None or abs(number) > 1_000_000:
            errors.append(f"implementation.{field} 必须为绝对值不超过 1000000 的有限数")
            return []
        result.append(number)
    return result


def _matmul(left_shape: list[int], right_shape: list[int], left: list[float], right: list[float]) -> list[float]:
    rows, shared = left_shape
    _, columns = right_shape
    output: list[float] = []
    for row in range(rows):
        for column in range(columns):
            value = 0.0
            for index in range(shared):
                value += left[row * shared + index] * right[index * columns + column]
            output.append(value)
    return output


def _validate_npu_implementation(value: Any, errors: list[str]) -> dict[str, Any]:
    implementation = _record(value)
    operation = _text(implementation.get("operation"), 80)
    if operation != "matmul":
        errors.append("implementation.operation 目前只能为 matmul")
    left_shape = _shape(implementation.get("left_shape"), "left_shape", errors)
    right_shape = _shape(implementation.get("right_shape"), "right_shape", errors)
    if left_shape and right_shape and left_shape[1] != right_shape[0]:
        errors.append("implementation.left_shape[1] 必须等于 right_shape[0]")
    left = _float_values(implementation.get("left_values"), "left_values", left_shape[0] * left_shape[1] if left_shape else 0, errors)
    right = _float_values(implementation.get("right_values"), "right_values", right_shape[0] * right_shape[1] if right_shape else 0, errors)
    tolerance = _finite_number(implementation.get("tolerance"))
    if tolerance is None or tolerance < 0.000001 or tolerance > 0.1:
        errors.append("implementation.tolerance 必须在 0.000001 到 0.1 之间")
        tolerance = 0.0001
    expected = _matmul(left_shape, right_shape, left, right) if left_shape and right_shape and left and right and left_shape[1] == right_shape[0] else []
    return {
        "operation": operation,
        "left_shape": left_shape,
        "right_shape": right_shape,
        "left_values": left,
        "right_values": right,
        "cpu_reference_output": expected,
        "tolerance": tolerance,
    }


def validate_device_adapter_spec(result: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate a model specification before it can materialize source code."""

    errors: list[str] = []
    normalized = dict(result)
    requested_unit = _text(context.get("requested_unit"), 40)
    model_input = _record(context.get("model_input_context"))
    dispatch = _record(model_input.get("dispatch_assessment"))
    allowed_evidence = set(_strings(model_input.get("allowed_evidence_ids"), limit=64, item_limit=240))
    if _text(normalized.get("schema_version"), 80) != "device_adapter_spec/v1":
        errors.append("schema_version 必须为 device_adapter_spec/v1")
    decision = _text(normalized.get("decision"), 80)
    if decision not in ADAPTER_DECISIONS:
        errors.append("decision 必须为 ready_for_materialization、needs_contract 或 not_supported")
    target_unit = _text(normalized.get("target_unit"), 40)
    if target_unit not in {*DEVICE_UNITS, "none"}:
        errors.append("target_unit 必须为 npu、gpu、dsp、media 或 none")
    if decision == "ready_for_materialization" and target_unit != requested_unit:
        errors.append("ready_for_materialization 必须针对本轮选择的硬件单元")
    adapter_kind = _text(normalized.get("adapter_kind"), 120)
    supported = {item["adapter_kind"] for item in supported_adapter_catalog(requested_unit)}
    if decision == "ready_for_materialization" and adapter_kind not in supported:
        errors.append("adapter_kind 不属于当前单元已登记的可物化模板")
    if decision == "ready_for_materialization":
        if _text(dispatch.get("decision"), 80) != "semantic_candidate" or _text(dispatch.get("validation_status"), 80) != "valid":
            errors.append("只有通过校验的 semantic_candidate 下发合同才能物化 adapter")
    summary = _text(normalized.get("summary"))
    rationale = _text(normalized.get("rationale"), 6_000)
    if not summary:
        errors.append("summary 不能为空")
    if not rationale:
        errors.append("rationale 不能为空")
    scope = _validate_scope(normalized.get("source_scope"), errors)
    evidence_ids = _validate_evidence(normalized.get("evidence_ids"), allowed_evidence, errors)
    if decision == "ready_for_materialization" and not evidence_ids:
        errors.append("ready_for_materialization 必须引用至少一条本轮证据")
    implementation: dict[str, Any] = {}
    if decision == "ready_for_materialization" and adapter_kind == GPU_ADAPTER_KIND:
        implementation = _validate_gpu_implementation(normalized.get("implementation"), errors)
    if decision == "ready_for_materialization" and adapter_kind == NPU_ADAPTER_KIND:
        implementation = _validate_npu_implementation(normalized.get("implementation"), errors)
    validated_decision = decision
    calibration_actions: list[str] = []
    if errors and decision == "ready_for_materialization":
        validated_decision = "needs_contract"
        adapter_kind = "none"
        implementation = {}
        calibration_actions.append("模型给出的 adapter 规格未通过模板、证据或输入校验，已降为 needs_contract；未生成代码或执行设备操作。")
    validated = {
        "schema_version": "device_adapter_spec/v1",
        "decision": validated_decision,
        "target_unit": target_unit,
        "adapter_kind": adapter_kind or "none",
        "summary": summary,
        "rationale": rationale,
        "source_scope": scope,
        "implementation": implementation,
        "verification_plan": _strings(normalized.get("verification_plan")),
        "risks": _strings(normalized.get("risks")),
        "evidence_ids": evidence_ids,
        "validation": {
            "status": "valid" if not errors else "invalid",
            "errors": errors,
            "calibration_actions": calibration_actions,
        },
    }
    return validated, errors


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _c_string_lines(value: str, indent: str = "        ") -> str:
    lines = value.splitlines(keepends=True)
    if not lines:
        return f'{indent}""'
    return "\n".join(f"{indent}{json.dumps(line)}" for line in lines)


def _replace_once(source: str, pattern: str, replacement: str, label: str) -> str:
    # A generated C string contains literal backslashes.  Supplying it as a
    # replacement string would make ``re`` interpret those escapes again.
    updated, count = re.subn(pattern, lambda _match: replacement, source, count=1, flags=re.DOTALL)
    if count != 1:
        raise PlatformError("DEVICE_ADAPTER_TEMPLATE_INVALID", f"{label} 模板标记不可用。", status_code=503)
    return updated


def _byte_array(name: str, values: list[int]) -> str:
    return f"    static const unsigned char {name}[] = {{\n        {', '.join(str(value) for value in values)}\n    }};"


def _c_float(value: float) -> str:
    rendered = format(value, ".9g")
    if "e" not in rendered.lower() and "." not in rendered:
        rendered += ".0"
    return f"{rendered}F"


def _float_array(name: str, values: list[float]) -> str:
    return f"    static const float {name}[] = {{\n        {', '.join(_c_float(value) for value in values)}\n    }};"


def _int_array(name: str, values: list[int]) -> str:
    return f"    static const int32_t {name}[] = {{{', '.join(str(value) for value in values)}}};"


def _render_gpu_source(base: str, implementation: dict[str, Any]) -> str:
    expression = str(implementation["fragment_expression"])
    fragment = "\n".join([
        "#version 300 es",
        "precision highp float;",
        "uniform sampler2D inputRgba;",
        "uniform float brightness;",
        "in vec2 vTexCoord;",
        "layout(location = 0) out vec4 outputRgba;",
        "void main() {",
        "  vec4 pixel = texture(inputRgba, vTexCoord);",
        f"  outputRgba = {expression};",
        "}",
        "",
    ])
    shader = "    static const char kFragmentShader[] =\n" + _c_string_lines(fragment) + ";\n\n    const GLuint vertex"
    source = _replace_once(
        base,
        r"    static const char kFragmentShader\[\] =\n.*?;\n\n    const GLuint vertex",
        shader,
        "GPU fragment shader",
    )
    source = _replace_once(source, r"    static const unsigned char kInput\[\] = \{\n.*?    \};", _byte_array("kInput", implementation["input_rgba"]), "GPU input")
    source = _replace_once(source, r"    static const unsigned char kExpected\[\] = \{\n.*?    \};", _byte_array("kExpected", implementation["expected_rgba"]), "GPU expected output")
    source = _replace_once(
        source,
        r'glUniform1f\(glGetUniformLocation\(program, "brightness"\), [^)]+\);',
        f'glUniform1f(glGetUniformLocation(program, "brightness"), {_c_float(float(implementation["brightness"]))});',
        "GPU brightness uniform",
    )
    source = _replace_once(source, r"GPU RGBA brightness offload: PASS", "HETERO_GPU_ADAPTER_PASS", "GPU pass marker")
    source = _replace_once(source, r"GPU RGBA brightness offload: FAIL", "HETERO_GPU_ADAPTER_FAIL", "GPU fail marker")
    return source


def _render_npu_source(base: str, implementation: dict[str, Any]) -> str:
    left_shape = [int(item) for item in implementation["left_shape"]]
    right_shape = [int(item) for item in implementation["right_shape"]]
    output_shape = [left_shape[0], right_shape[1]]
    source = _replace_once(source=base, pattern=r"    static const int32_t kLeftShape\[\] = \{.*?\};", replacement=_int_array("kLeftShape", left_shape), label="NPU left shape")
    source = _replace_once(source, r"    static const int32_t kRightShape\[\] = \{.*?\};", _int_array("kRightShape", right_shape), "NPU right shape")
    source = _replace_once(source, r"    static const int32_t kOutputShape\[\] = \{.*?\};", _int_array("kOutputShape", output_shape), "NPU output shape")
    source = _replace_once(source, r"    static const float kLeft\[\] = \{.*?\};", _float_array("kLeft", implementation["left_values"]), "NPU left values")
    source = _replace_once(source, r"    static const float kRight\[\] = \{.*?\};", _float_array("kRight", implementation["right_values"]), "NPU right values")
    source = _replace_once(source, r"    static const float kExpected\[\] = \{.*?\};", _float_array("kExpected", implementation["cpu_reference_output"]), "NPU CPU reference")
    copy_loop = "\n".join([
        f"    for (Size index = 0; index < {len(implementation['left_values'])}; ++index) {{",
        "        left[index] = kLeft[index];",
        "    }",
        f"    for (Size index = 0; index < {len(implementation['right_values'])}; ++index) {{",
        "        right[index] = kRight[index];",
        "    }",
    ])
    source = _replace_once(
        source,
        r"    for \(Size index = 0; index < 6; \+\+index\) \{\n        left\[index\] = kLeft\[index\];\n        right\[index\] = kRight\[index\];\n    \}",
        copy_loop,
        "NPU input copy",
    )
    source = _replace_once(
        source,
        r"for \(Size index = 0; index < 4; \+\+index\) \{",
        f"for (Size index = 0; index < {len(implementation['cpu_reference_output'])}; ++index) {{",
        "NPU result comparison loop",
    )
    source = _replace_once(
        source,
        r"if \(difference > 0\.0001F\) \{",
        f"if (difference > {_c_float(float(implementation['tolerance']))}) {{",
        "NPU tolerance",
    )
    source = _replace_once(source, r"NPU NNRT MatMul accelerator offload: PASS", "HETERO_NPU_ADAPTER_PASS", "NPU pass marker")
    source = _replace_once(source, r"NPU NNRT MatMul accelerator offload: FAIL", "HETERO_NPU_ADAPTER_FAIL", "NPU fail marker")
    return source


def materialize_device_adapter(
    workspace_root: Path,
    state_root: Path,
    *,
    context: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Render exactly one immutable source file from a validated data spec."""

    validation = _record(spec.get("validation"))
    if spec.get("decision") != "ready_for_materialization" or validation.get("status") != "valid":
        return {
            "status": "evidence_required",
            "reason_zh": "Adapter 规格尚未形成可物化的受控模板；未生成代码。",
        }
    kind = _text(spec.get("adapter_kind"), 160)
    implementation = _record(spec.get("implementation"))
    key = sha256_json({"context_sha256": context.get("context_sha256"), "spec": spec})[:24]
    resolved_state_root = state_root.resolve()
    root = (resolved_state_root / "generated-device-adapters" / key).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if kind == GPU_ADAPTER_KIND:
        template = workspace_root / GPU_BASE_SOURCE
        filename = "generated_gpu_adapter.c"
        source = _render_gpu_source(template.read_text(encoding="utf-8"), implementation)
    elif kind == NPU_ADAPTER_KIND:
        template = workspace_root / NPU_BASE_SOURCE
        filename = "generated_npu_adapter.c"
        source = _render_npu_source(template.read_text(encoding="utf-8"), implementation)
    else:
        return {
            "status": "evidence_required",
            "reason_zh": "当前单元没有已登记的可物化 Adapter 模板；未生成代码。",
        }
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise PlatformError("DEVICE_ADAPTER_SOURCE_TOO_LARGE", "生成的 Adapter 源码超过受控大小上限。", status_code=422)
    target = root / filename
    _atomic_write_text(target, source)
    return {
        "status": "ready",
        "adapter_key": key,
        "adapter_kind": kind,
        "source_relative_path": str(target.relative_to(resolved_state_root)),
        "source_sha256": sha256_file(target),
        "source_bytes": len(encoded),
        "source": source,
        "source_generation_zh": "由平台可信 C host 模板与模型校验后的结构化参数物化；模型未提供可执行 shell/HDC 命令。",
    }


def _adapter_source_path(state_root: Path, materialization: dict[str, Any]) -> Path:
    relative = _text(materialization.get("source_relative_path"), 1_024)
    root = (state_root / "generated-device-adapters").resolve()
    candidate = (state_root / relative).resolve()
    if not relative or not candidate.is_relative_to(root):
        raise PlatformError("DEVICE_ADAPTER_SOURCE_INVALID", "生成 Adapter 的源码路径不在受控工件目录内。", status_code=422)
    if not candidate.is_file():
        raise PlatformError("DEVICE_ADAPTER_SOURCE_MISSING", "生成 Adapter 的源码文件已不存在。", status_code=409)
    expected = _text(materialization.get("source_sha256"), 80)
    if not expected or sha256_file(candidate) != expected:
        raise PlatformError("DEVICE_ADAPTER_SOURCE_CHANGED", "生成 Adapter 源码哈希已变化，拒绝运行。", status_code=409)
    return candidate


def _toolchain(workspace_root: Path) -> tuple[Path, Path]:
    root = llvm_bin_root()
    clang = root / "clang"
    lld = root / "ld.lld"
    if not clang.is_file() or not lld.is_file() or not os.access(clang, os.X_OK) or not os.access(lld, os.X_OK):
        raise PlatformError(
            "DEVICE_ADAPTER_TOOLCHAIN_UNAVAILABLE",
            "未找到生成 Adapter 所需的 LLVM clang/ld.lld。请在 Linux Build Worker 上执行 "
            "./deploy/linux/heteroctl restart --llvm-bin-root <包含 clang、clang++、ld.lld 的目录>。",
            status_code=503,
            details={"llvm_bin_root": str(root)},
        )
    return clang, lld


def _bounded_process_output(completed: Any) -> dict[str, Any]:
    return {
        "return_code": completed.returncode,
        "stdout": _bounded(_clean_output(completed.stdout), 12 * 1024),
        "stderr": _bounded(_clean_output(completed.stderr), 12 * 1024),
    }


def _pull_device_libraries(hdc_binary: Path, serial: str, libraries: list[tuple[str, Path]]) -> None:
    for remote, local in libraries:
        local.parent.mkdir(parents=True, exist_ok=True)
        _run_hdc(hdc_binary, ("file", "recv", remote, str(local)), serial=serial, timeout_seconds=90)


def _compile_gpu_adapter(workspace_root: Path, hdc_binary: Path, serial: str, source: Path, run_root: Path) -> tuple[Path | None, dict[str, Any]]:
    clang, lld = _toolchain(workspace_root)
    libs = run_root / "device-gpu-libs"
    _pull_device_libraries(hdc_binary, serial, [
        ("/system/lib64/libEGL.so", libs / "libEGL.so"),
        ("/system/lib64/libGLESv3.so", libs / "libGLESv3.so"),
        ("/system/lib64/libc.so", libs / "libc.so"),
    ])
    object_path = run_root / "generated_gpu_adapter.o"
    binary_path = run_root / "generated_gpu_adapter"
    sdk_root = openharmony_sdk_root()
    include_egl = sdk_root / "interface/sdk_c/third_party/egl"
    include_gles = sdk_root / "interface/sdk_c/third_party/openGLES"
    compile_result = _run_process(
        [str(clang), "--target=aarch64-linux-musl", "-O2", "-std=c17", "-fPIE", "-ffreestanding", "-fno-stack-protector", "-fno-builtin", "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-isystem", str(include_egl), "-isystem", str(include_gles), "-c", str(source), "-o", str(object_path)],
        timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS,
        cwd=run_root,
    )
    detail = {"compile": _bounded_process_output(compile_result)}
    if compile_result.returncode != 0:
        return None, detail
    link_result = _run_process(
        [str(lld), LLD_THREAD_ARGUMENT, "-m", "aarch64linux", "-pie", "-z", "max-page-size=4096", "-e", "_start", "-dynamic-linker", "/lib/ld-musl-aarch64.so.1", "-rpath", "/system/lib64", "-rpath", "/vendor/lib64", "-o", str(binary_path), str(object_path), str(libs / "libEGL.so"), str(libs / "libGLESv3.so"), str(libs / "libc.so")],
        timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS,
        cwd=run_root,
    )
    detail["link"] = _bounded_process_output(link_result)
    return (binary_path if link_result.returncode == 0 and binary_path.is_file() else None), detail


def _compile_npu_adapter(workspace_root: Path, hdc_binary: Path, serial: str, source: Path, run_root: Path) -> tuple[Path | None, dict[str, Any]]:
    clang, lld = _toolchain(workspace_root)
    libs = run_root / "device-nnrt-libs"
    _pull_device_libraries(hdc_binary, serial, [
        ("/system/lib64/ndk/libneural_network_runtime.so", libs / "libneural_network_runtime.so"),
        ("/system/lib64/ndk/libneural_network_core.so", libs / "libneural_network_core.so"),
        ("/system/lib64/libc.so", libs / "libc.so"),
    ])
    object_path = run_root / "generated_npu_adapter.o"
    binary_path = run_root / "generated_npu_adapter"
    include_nnrt = openharmony_sdk_root() / "interface/sdk_c/ai"
    compile_result = _run_process(
        [str(clang), "--target=aarch64-linux-musl", "-O2", "-std=gnu2x", "-fPIE", "-ffreestanding", "-fno-stack-protector", "-fno-builtin", "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-isystem", str(include_nnrt), "-c", str(source), "-o", str(object_path)],
        timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS,
        cwd=run_root,
    )
    detail = {"compile": _bounded_process_output(compile_result)}
    if compile_result.returncode != 0:
        return None, detail
    link_result = _run_process(
        [str(lld), LLD_THREAD_ARGUMENT, "-m", "aarch64linux", "-pie", "-z", "max-page-size=4096", "-e", "_start", "-dynamic-linker", "/lib/ld-musl-aarch64.so.1", "-rpath", "/system/lib64/ndk", "-rpath", "/system/lib64", "-rpath", "/vendor/lib64", "-o", str(binary_path), str(object_path), str(libs / "libneural_network_runtime.so"), str(libs / "libneural_network_core.so"), str(libs / "libc.so")],
        timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS,
        cwd=run_root,
    )
    detail["link"] = _bounded_process_output(link_result)
    return (binary_path if link_result.returncode == 0 and binary_path.is_file() else None), detail


def _cleanup_remote_binary(hdc_binary: Path, serial: str, remote_path: str) -> dict[str, Any]:
    try:
        completed = _run_hdc(hdc_binary, ("shell", "rm", "-f", remote_path), serial=serial, require_success=False)
        return {
            "attempted": True,
            "succeeded": completed.returncode == 0,
            "path": remote_path,
            "stdout": _bounded(_clean_output(completed.stdout), 2_048),
            "stderr": _bounded(_clean_output(completed.stderr), 2_048),
        }
    except PlatformError as exc:
        # Cleanup is evidence too, but must never erase the original compile or
        # runtime result when the device disconnects during teardown.
        return {
            "attempted": True,
            "succeeded": False,
            "path": remote_path,
            "error_code": exc.code,
            "stderr": _bounded(exc.message, 2_048),
        }


def _safe_adapter_slug(adapter_artifact_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", adapter_artifact_id.casefold())[:24]
    return slug or sha256_json({"adapter_artifact_id": adapter_artifact_id})[:20]


def run_generated_device_adapter(
    workspace_root: Path,
    state_root: Path,
    *,
    adapter_artifact: dict[str, Any],
    probe_artifact: dict[str, Any],
) -> dict[str, Any]:
    """Compile and run one immutable template-generated adapter on a device."""

    spec = _record(adapter_artifact.get("result"))
    materialization = _record(adapter_artifact.get("materialization"))
    unit = _text(adapter_artifact.get("requested_unit"), 40)
    if materialization.get("status") != "ready" or spec.get("decision") != "ready_for_materialization":
        raise PlatformError("DEVICE_ADAPTER_NOT_EXECUTABLE", "当前 Adapter 尚未形成可执行的受控模板。", status_code=422)
    source = _adapter_source_path(state_root, materialization)
    serial = _safe_serial(str(_record(probe_artifact.get("device")).get("serial") or ""))
    hdc_binary = resolve_hdc_binary(workspace_root)
    if serial not in _available_devices(hdc_binary):
        raise PlatformError("DEVICE_NOT_CONNECTED", "执行前设备已断开 HDC 连接。", status_code=409, details={"device_serial": serial})
    slug = _safe_adapter_slug(str(adapter_artifact.get("artifact_id") or "adapter"))
    run_root = state_root / "generated-device-adapter-runs" / slug
    run_root.mkdir(parents=True, exist_ok=True)
    remote_path = (GPU_REMOTE_PREFIX if unit == "gpu" else NPU_REMOTE_PREFIX) + slug
    started = time.monotonic()
    compile_detail: dict[str, Any] = {}
    stdout = ""
    stderr = ""
    return_code: int | None = None
    timeout = False
    cleanup: dict[str, Any] = {"attempted": False}
    try:
        if unit == "gpu" and spec.get("adapter_kind") == GPU_ADAPTER_KIND:
            binary, compile_detail = _compile_gpu_adapter(workspace_root, hdc_binary, serial, source, run_root)
        elif unit == "npu" and spec.get("adapter_kind") == NPU_ADAPTER_KIND:
            binary, compile_detail = _compile_npu_adapter(workspace_root, hdc_binary, serial, source, run_root)
        else:
            raise PlatformError("DEVICE_ADAPTER_TEMPLATE_UNSUPPORTED", "该 Adapter 的单元或模板当前没有注册运行器。", status_code=422)
        if binary is None:
            return {
                "schema_version": "generated_device_adapter_run/v1",
                "adapter_artifact_id": adapter_artifact.get("artifact_id"),
                "probe_artifact_id": probe_artifact.get("artifact_id"),
                "requested_unit": unit,
                "device": {"serial": serial},
                "status": "failed",
                "execution": {"duration_ms": round((time.monotonic() - started) * 1000, 3), "return_code": None, "timed_out": False, "stdout": "", "stderr": ""},
                "compile": compile_detail,
                "cleanup": cleanup,
                "observed_evidence": {"source_sha256": materialization.get("source_sha256"), "pass_marker_observed": False},
                "summary_zh": "生成的 Adapter 未能通过本机交叉编译或链接；设备未执行该二进制。",
                "evidence_calibration": {
                    "adds": ["记录了受控 Adapter 的编译/链接失败事实"],
                    "does_not_add": ["不说明设备不支持目标单元", "不说明热点不适合卸载"],
                },
            }
        _run_hdc(hdc_binary, ("file", "send", str(binary), remote_path), serial=serial, timeout_seconds=90)
        completed = _run_hdc(hdc_binary, ("shell", f"chmod 0755 {remote_path} && {remote_path}"), serial=serial, timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS, require_success=False)
        stdout = _bounded(_clean_output(completed.stdout))
        stderr = _bounded(_clean_output(completed.stderr))
        return_code = completed.returncode
    except PlatformError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        stdout = _bounded(str(details.get("stdout") or stdout))
        stderr = _bounded(str(details.get("stderr") or exc.message))
        timeout = exc.code == "DEVICE_COMMAND_TIMEOUT"
    finally:
        cleanup = _cleanup_remote_binary(hdc_binary, serial, remote_path)

    marker = "HETERO_GPU_ADAPTER_PASS" if unit == "gpu" else "HETERO_NPU_ADAPTER_PASS"
    unsupported = unit == "npu" and "NPU NNRT MatMul availability: NOT_SUPPORTED" in stdout
    passed = not timeout and return_code == 0 and marker in stdout
    status = "passed" if passed else "unsupported" if unsupported else "failed"
    observations: dict[str, Any] = {
        "source_sha256": materialization.get("source_sha256"),
        "pass_marker_observed": marker in stdout,
        "adapter_kind": spec.get("adapter_kind"),
    }
    if unit == "npu":
        observations.update({
            "device_enumeration_passed": "NPU NNRT device enumeration: PASS" in stdout,
            "graph_finish_passed": "NPU NNRT graph: FINISH PASS" in stdout,
            "operation_availability": "not_supported" if unsupported else "supported" if "NPU NNRT MatMul availability: PASS" in stdout else "unknown",
            "compilation_passed": "NPU NNRT compilation: PASS" in stdout,
            "execution_passed": "NPU NNRT execution: PASS" in stdout,
            "result_comparison_passed": "NPU NNRT result comparison: PASS" in stdout,
        })
    summary = (
        f"已在连接设备上生成、编译并运行受控 {unit.upper()} Adapter，固定输入输出比较通过。"
        if passed
        else "选定 NPU 明确拒绝该精确 MatMul 图；这是该图的设备观察，不代表其他图或热点不可用。"
        if unsupported
        else f"已尝试生成并运行受控 {unit.upper()} Adapter，但未通过本次功能验证；可由页面助手读取真实输出后修复规格或停止。"
    )
    return {
        "schema_version": "generated_device_adapter_run/v1",
        "adapter_artifact_id": adapter_artifact.get("artifact_id"),
        "probe_artifact_id": probe_artifact.get("artifact_id"),
        "requested_unit": unit,
        "device": {"serial": serial},
        "status": status,
        "execution": {
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "return_code": return_code,
            "timed_out": timeout,
            "stdout": stdout,
            "stderr": stderr,
        },
        "compile": compile_detail,
        "cleanup": cleanup,
        "observed_evidence": observations,
        "summary_zh": summary,
        "evidence_calibration": {
            "adds": [
                "该受控 Adapter 的源码哈希、交叉编译结果和本次设备功能比较事实",
                "固定输入输出是否通过由设备运行结果决定",
            ],
            "does_not_add": [
                "完整热点已经集成到生产调用链",
                "端到端性能、功耗、搬运成本或生产输入规模收益",
                "未测试 API、格式、shape 或其他硬件单元的可用性",
            ],
            "recommendation_effect": "不会自动提升原策略推理的 recommendation、confidence 或 evidence_status。",
        },
    }
