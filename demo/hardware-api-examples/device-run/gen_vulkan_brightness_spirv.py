#!/usr/bin/env python3
"""Reference hand-written SPIR-V generator (not used by the build).

The production build uses glslang to compile brightness.comp into SPIR-V.
This script is kept as a reference for how the same shader can be emitted
without a GLSL compiler.
"""
import struct
import sys
from pathlib import Path


class SpirvBuilder:
    MAGIC = 0x07230203
    VERSION = 0x00010300  # SPIR-V 1.0
    GENERATOR = 0
    SCHEMA = 0

    # Opcode values from SPIR-V spec (core)
    OP_NOP = 0
    OP_NAME = 5
    OP_DECORATE = 71
    OP_MEMBER_DECORATE = 72
    OP_EXT_INST_IMPORT = 11
    OP_EXT_INST = 12
    OP_MEMORY_MODEL = 14
    OP_ENTRY_POINT = 15
    OP_EXECUTION_MODE = 16
    OP_CAPABILITY = 17
    OP_TYPE_VOID = 19
    OP_TYPE_BOOL = 20
    OP_TYPE_INT = 21
    OP_TYPE_FLOAT = 22
    OP_TYPE_VECTOR = 23
    OP_TYPE_RUNTIME_ARRAY = 29
    OP_TYPE_STRUCT = 30
    OP_TYPE_POINTER = 32
    OP_TYPE_FUNCTION = 33
    OP_CONSTANT = 43
    OP_FUNCTION = 54
    OP_FUNCTION_END = 56
    OP_FUNCTION_CALL = 57
    OP_VARIABLE = 59
    OP_LOAD = 61
    OP_STORE = 62
    OP_ACCESS_CHAIN = 65
    OP_DECORATE_BRA = 66  # not used
    OP_CONVERT_F_TO_U = 109
    OP_CONVERT_U_TO_F = 112
    OP_BITWISE_OR = 197
    OP_BITWISE_AND = 198
    OP_SHIFT_LEFT_LOGICAL = 196
    OP_SHIFT_RIGHT_LOGICAL = 194
    OP_FADD = 129
    OP_UGREATER_THAN_EQUAL = 182
    OP_BRANCH = 249
    OP_BRANCH_CONDITIONAL = 250
    OP_LABEL = 248
    OP_RETURN = 253
    OP_SELECTION_MERGE = 250  # same opcode? No, selection merge is 247, branch conditional 250
    # Fix: correct opcodes
    OP_SELECTION_MERGE = 247

    # Decoration literals
    DEC_BUILTIN = 11
    DEC_DESCRIPTOR_SET = 34
    DEC_BINDING = 33
    DEC_BLOCK = 2
    DEC_BUFFER_BLOCK = 3
    DEC_OFFSET = 35
    DEC_ARRAY_STRIDE = 6

    # BuiltIns
    BUILTIN_GLOBAL_INVOCATION_ID = 28

    # Capability
    CAP_SHADER = 1

    # Addressing / Memory model
    ADDR_LOGICAL = 0
    MEM_GLSL450 = 1

    # Execution model
    EXEC_GL_COMPUTE = 5

    # Storage classes
    SC_INPUT = 1
    SC_UNIFORM_CONSTANT = 0
    SC_PUSH_CONSTANT = 9
    SC_UNIFORM = 2  # Vulkan 1.0 SSBO uses Uniform + BufferBlock

    # Function control
    FUNC_CONTROL_NONE = 0

    def __init__(self):
        self.words = []
        self.id_bound = 1
        self.ext_glsl_id = None

    def new_id(self):
        i = self.id_bound
        self.id_bound += 1
        return i

    def emit_word(self, word):
        self.words.append(word & 0xFFFFFFFF)

    def emit_opcode(self, opcode, count):
        # count = word count including opcode word
        self.emit_word((count << 16) | opcode)

    def emit_string_words(self, s):
        encoded = s.encode('utf-8') + b'\x00'
        while len(encoded) % 4 != 0:
            encoded += b'\x00'
        for i in range(0, len(encoded), 4):
            self.emit_word(struct.unpack('<I', encoded[i:i+4])[0])

    def instr_capability(self, cap):
        self.emit_opcode(self.OP_CAPABILITY, 2)
        self.emit_word(cap)

    def instr_ext_inst_import(self, result_id, name):
        self.emit_opcode(self.OP_EXT_INST_IMPORT, 2 + (len(name) + 4) // 4)
        self.emit_word(result_id)
        self.emit_string_words(name)

    def instr_memory_model(self, addr, mem):
        self.emit_opcode(self.OP_MEMORY_MODEL, 3)
        self.emit_word(addr)
        self.emit_word(mem)

    def instr_entry_point(self, exec_model, func_id, name, *interface_ids):
        self.emit_opcode(self.OP_ENTRY_POINT, 3 + (len(name) + 4) // 4 + len(interface_ids))
        self.emit_word(exec_model)
        self.emit_word(func_id)
        self.emit_string_words(name)
        for iid in interface_ids:
            self.emit_word(iid)

    def instr_execution_mode(self, func_id, mode, *args):
        self.emit_opcode(self.OP_EXECUTION_MODE, 3 + len(args))
        self.emit_word(func_id)
        self.emit_word(mode)
        for a in args:
            self.emit_word(a)

    def instr_name(self, target_id, name):
        self.emit_opcode(self.OP_NAME, 2 + (len(name) + 4) // 4)
        self.emit_word(target_id)
        self.emit_string_words(name)

    def instr_decorate(self, target_id, decoration, *args):
        self.emit_opcode(self.OP_DECORATE, 3 + len(args))
        self.emit_word(target_id)
        self.emit_word(decoration)
        for a in args:
            self.emit_word(a)

    def instr_member_decorate(self, struct_id, member, decoration, *args):
        self.emit_opcode(self.OP_MEMBER_DECORATE, 4 + len(args))
        self.emit_word(struct_id)
        self.emit_word(member)
        self.emit_word(decoration)
        for a in args:
            self.emit_word(a)

    def instr_type_void(self, result_id):
        self.emit_opcode(self.OP_TYPE_VOID, 2)
        self.emit_word(result_id)

    def instr_type_bool(self, result_id):
        self.emit_opcode(self.OP_TYPE_BOOL, 2)
        self.emit_word(result_id)

    def instr_type_int(self, result_id, width, signedness):
        self.emit_opcode(self.OP_TYPE_INT, 4)
        self.emit_word(result_id)
        self.emit_word(width)
        self.emit_word(signedness)

    def instr_type_float(self, result_id, width):
        self.emit_opcode(self.OP_TYPE_FLOAT, 3)
        self.emit_word(result_id)
        self.emit_word(width)

    def instr_type_vector(self, result_id, comp_type_id, count):
        self.emit_opcode(self.OP_TYPE_VECTOR, 4)
        self.emit_word(result_id)
        self.emit_word(comp_type_id)
        self.emit_word(count)

    def instr_type_runtime_array(self, result_id, elem_type_id):
        self.emit_opcode(self.OP_TYPE_RUNTIME_ARRAY, 3)
        self.emit_word(result_id)
        self.emit_word(elem_type_id)

    def instr_type_struct(self, result_id, *member_type_ids):
        self.emit_opcode(self.OP_TYPE_STRUCT, 2 + len(member_type_ids))
        self.emit_word(result_id)
        for mtid in member_type_ids:
            self.emit_word(mtid)

    def instr_type_pointer(self, result_id, storage_class, pointee_type_id):
        self.emit_opcode(self.OP_TYPE_POINTER, 4)
        self.emit_word(result_id)
        self.emit_word(storage_class)
        self.emit_word(pointee_type_id)

    def instr_type_function(self, result_id, return_type_id, *param_type_ids):
        self.emit_opcode(self.OP_TYPE_FUNCTION, 3 + len(param_type_ids))
        self.emit_word(result_id)
        self.emit_word(return_type_id)
        for ptid in param_type_ids:
            self.emit_word(ptid)

    def instr_constant(self, result_type_id, result_id, value):
        self.emit_opcode(self.OP_CONSTANT, 4)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(value)

    def instr_variable(self, result_type_id, result_id, storage_class, *init_id):
        count = 4
        if init_id:
            count += 1
        self.emit_opcode(self.OP_VARIABLE, count)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(storage_class)
        if init_id:
            self.emit_word(init_id[0])

    def instr_function(self, return_type_id, func_id, func_control, func_type_id):
        self.emit_opcode(self.OP_FUNCTION, 5)
        self.emit_word(return_type_id)
        self.emit_word(func_id)
        self.emit_word(func_control)
        self.emit_word(func_type_id)

    def instr_function_end(self):
        self.emit_opcode(self.OP_FUNCTION_END, 1)

    def instr_label(self, label_id):
        self.emit_opcode(self.OP_LABEL, 2)
        self.emit_word(label_id)

    def instr_return(self):
        self.emit_opcode(self.OP_RETURN, 1)

    def instr_branch(self, label_id):
        self.emit_opcode(self.OP_BRANCH, 2)
        self.emit_word(label_id)

    def instr_branch_conditional(self, condition_id, true_label, false_label):
        self.emit_opcode(self.OP_BRANCH_CONDITIONAL, 4)
        self.emit_word(condition_id)
        self.emit_word(true_label)
        self.emit_word(false_label)

    def instr_selection_merge(self, merge_label, selection_control=0):
        self.emit_opcode(self.OP_SELECTION_MERGE, 3)
        self.emit_word(merge_label)
        self.emit_word(selection_control)

    def instr_load(self, result_type_id, result_id, ptr_id):
        self.emit_opcode(self.OP_LOAD, 4)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(ptr_id)

    def instr_store(self, ptr_id, value_id):
        self.emit_opcode(self.OP_STORE, 3)
        self.emit_word(ptr_id)
        self.emit_word(value_id)

    def instr_access_chain(self, result_type_id, result_id, base_id, *index_ids):
        self.emit_opcode(self.OP_ACCESS_CHAIN, 4 + len(index_ids))
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(base_id)
        for iid in index_ids:
            self.emit_word(iid)

    def instr_convert_f_to_u(self, result_type_id, result_id, float_id):
        self.emit_opcode(self.OP_CONVERT_F_TO_U, 4)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(float_id)

    def instr_convert_u_to_f(self, result_type_id, result_id, uint_id):
        self.emit_opcode(self.OP_CONVERT_U_TO_F, 4)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(uint_id)

    def instr_fadd(self, result_type_id, result_id, a_id, b_id):
        self.emit_opcode(self.OP_FADD, 5)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(a_id)
        self.emit_word(b_id)

    def instr_bitwise_or(self, result_type_id, result_id, a_id, b_id):
        self.emit_opcode(self.OP_BITWISE_OR, 5)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(a_id)
        self.emit_word(b_id)

    def instr_bitwise_and(self, result_type_id, result_id, a_id, b_id):
        self.emit_opcode(self.OP_BITWISE_AND, 5)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(a_id)
        self.emit_word(b_id)

    def instr_shift_left_logical(self, result_type_id, result_id, base_id, shift_id):
        self.emit_opcode(self.OP_SHIFT_LEFT_LOGICAL, 5)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(base_id)
        self.emit_word(shift_id)

    def instr_shift_right_logical(self, result_type_id, result_id, base_id, shift_id):
        self.emit_opcode(self.OP_SHIFT_RIGHT_LOGICAL, 5)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(base_id)
        self.emit_word(shift_id)

    def instr_u_greater_than_equal(self, result_type_id, result_id, a_id, b_id):
        self.emit_opcode(self.OP_UGREATER_THAN_EQUAL, 5)
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(a_id)
        self.emit_word(b_id)

    def instr_ext_inst(self, result_type_id, result_id, set_id, instruction, *operand_ids):
        self.emit_opcode(self.OP_EXT_INST, 5 + len(operand_ids))
        self.emit_word(result_type_id)
        self.emit_word(result_id)
        self.emit_word(set_id)
        self.emit_word(instruction)
        for oid in operand_ids:
            self.emit_word(oid)

    def build(self):
        header = [
            self.MAGIC,
            self.VERSION,
            self.GENERATOR,
            self.id_bound,
            self.SCHEMA,
        ]
        return b''.join(struct.pack('<I', w) for w in header) + b''.join(struct.pack('<I', w) for w in self.words)


def build_brightness_shader():
    b = SpirvBuilder()

    # IDs
    id_void = b.new_id()
    id_bool = b.new_id()
    id_uint = b.new_id()
    id_float = b.new_id()
    id_uvec3 = b.new_id()
    id_func_void = b.new_id()
    id_rtarr_uint = b.new_id()
    id_struct_src = b.new_id()
    id_struct_dst = b.new_id()
    id_struct_params = b.new_id()
    id_ptr_input_uvec3 = b.new_id()
    id_ptr_input_uint = b.new_id()
    id_ptr_ssbo_struct_src = b.new_id()
    id_ptr_ssbo_struct_dst = b.new_id()
    id_ptr_ssbo_uint = b.new_id()
    id_ptr_push_struct = b.new_id()
    id_ptr_push_float = b.new_id()
    id_ptr_push_uint = b.new_id()
    id_gl_GlobalInvocationID = b.new_id()
    id_src = b.new_id()
    id_dst = b.new_id()
    id_params = b.new_id()
    id_main = b.new_id()
    id_entry = b.new_id()
    id_body = b.new_id()
    id_exit = b.new_id()

    # Constants
    id_uint_0 = b.new_id()
    id_uint_1 = b.new_id()
    id_uint_2 = b.new_id()
    id_uint_3 = b.new_id()
    id_uint_8 = b.new_id()
    id_uint_16 = b.new_id()
    id_uint_24 = b.new_id()
    id_uint_255 = b.new_id()
    id_uint_64 = b.new_id()
    id_float_0 = b.new_id()
    id_float_255 = b.new_id()

    b.ext_glsl_id = b.new_id()

    # Header declarations
    b.instr_capability(b.CAP_SHADER)
    b.instr_ext_inst_import(b.ext_glsl_id, "GLSL.std.450")
    b.instr_memory_model(b.ADDR_LOGICAL, b.MEM_GLSL450)
    b.instr_entry_point(b.EXEC_GL_COMPUTE, id_main, "main", id_gl_GlobalInvocationID)
    # LocalSize 64 1 1
    b.instr_execution_mode(id_main, 17, 64, 1, 1)

    # Names (optional but helpful for debugging)
    b.instr_name(id_main, "main")
    b.instr_name(id_gl_GlobalInvocationID, "gl_GlobalInvocationID")
    b.instr_name(id_src, "src")
    b.instr_name(id_dst, "dst")
    b.instr_name(id_params, "params")

    # Decorations
    b.instr_decorate(id_gl_GlobalInvocationID, b.DEC_BUILTIN, b.BUILTIN_GLOBAL_INVOCATION_ID)
    b.instr_decorate(id_src, b.DEC_DESCRIPTOR_SET, 0)
    b.instr_decorate(id_src, b.DEC_BINDING, 0)
    b.instr_decorate(id_dst, b.DEC_DESCRIPTOR_SET, 0)
    b.instr_decorate(id_dst, b.DEC_BINDING, 1)
    # Storage buffer structs need BufferBlock (deprecated but required for Vulkan 1.0 SSBO)
    b.instr_decorate(id_struct_src, b.DEC_BUFFER_BLOCK)
    b.instr_decorate(id_struct_dst, b.DEC_BUFFER_BLOCK)
    b.instr_member_decorate(id_struct_src, 0, b.DEC_OFFSET, 0)
    b.instr_member_decorate(id_struct_dst, 0, b.DEC_OFFSET, 0)
    b.instr_decorate(id_rtarr_uint, b.DEC_ARRAY_STRIDE, 4)
    # Push constant struct uses Block
    b.instr_decorate(id_struct_params, b.DEC_BLOCK)
    b.instr_member_decorate(id_struct_params, 0, b.DEC_OFFSET, 0)
    b.instr_member_decorate(id_struct_params, 1, b.DEC_OFFSET, 4)

    # Types and constants
    b.instr_type_void(id_void)
    b.instr_type_bool(id_bool)
    b.instr_type_int(id_uint, 32, 0)
    b.instr_type_float(id_float, 32)
    b.instr_type_vector(id_uvec3, id_uint, 3)
    b.instr_type_function(id_func_void, id_void)
    b.instr_type_runtime_array(id_rtarr_uint, id_uint)
    b.instr_type_struct(id_struct_src, id_rtarr_uint)
    b.instr_type_struct(id_struct_dst, id_rtarr_uint)
    b.instr_type_struct(id_struct_params, id_float, id_uint)
    b.instr_type_pointer(id_ptr_input_uvec3, b.SC_INPUT, id_uvec3)
    b.instr_type_pointer(id_ptr_input_uint, b.SC_INPUT, id_uint)
    b.instr_type_pointer(id_ptr_ssbo_struct_src, b.SC_UNIFORM, id_struct_src)
    b.instr_type_pointer(id_ptr_ssbo_struct_dst, b.SC_UNIFORM, id_struct_dst)
    b.instr_type_pointer(id_ptr_ssbo_uint, b.SC_UNIFORM, id_uint)
    b.instr_type_pointer(id_ptr_push_struct, b.SC_PUSH_CONSTANT, id_struct_params)
    b.instr_type_pointer(id_ptr_push_float, b.SC_PUSH_CONSTANT, id_float)
    b.instr_type_pointer(id_ptr_push_uint, b.SC_PUSH_CONSTANT, id_uint)

    b.instr_constant(id_uint, id_uint_0, 0)
    b.instr_constant(id_uint, id_uint_1, 1)
    b.instr_constant(id_uint, id_uint_2, 2)
    b.instr_constant(id_uint, id_uint_3, 3)
    b.instr_constant(id_uint, id_uint_8, 8)
    b.instr_constant(id_uint, id_uint_16, 16)
    b.instr_constant(id_uint, id_uint_24, 24)
    b.instr_constant(id_uint, id_uint_255, 255)
    b.instr_constant(id_uint, id_uint_64, 64)
    b.instr_constant(id_float, id_float_0, struct.unpack('<I', struct.pack('<f', 0.0))[0])
    b.instr_constant(id_float, id_float_255, struct.unpack('<I', struct.pack('<f', 255.0))[0])

    # Variables
    b.instr_variable(id_ptr_input_uvec3, id_gl_GlobalInvocationID, b.SC_INPUT)
    b.instr_variable(id_ptr_ssbo_struct_src, id_src, b.SC_UNIFORM)
    b.instr_variable(id_ptr_ssbo_struct_dst, id_dst, b.SC_UNIFORM)
    b.instr_variable(id_ptr_push_struct, id_params, b.SC_PUSH_CONSTANT)

    # Function main
    b.instr_function(id_void, id_main, b.FUNC_CONTROL_NONE, id_func_void)
    b.instr_label(id_entry)

    # gid = gl_GlobalInvocationID.x
    id_gid_ptr = b.new_id()
    b.instr_access_chain(id_ptr_input_uint, id_gid_ptr, id_gl_GlobalInvocationID, id_uint_0)
    id_gid = b.new_id()
    b.instr_load(id_uint, id_gid, id_gid_ptr)

    # count = params.count (member 1)
    id_count_ptr = b.new_id()
    b.instr_access_chain(id_ptr_push_uint, id_count_ptr, id_params, id_uint_1)
    id_count = b.new_id()
    b.instr_load(id_uint, id_count, id_count_ptr)

    # if gid >= count exit
    id_cmp = b.new_id()
    b.instr_u_greater_than_equal(id_bool, id_cmp, id_gid, id_count)
    b.instr_selection_merge(id_exit)
    b.instr_branch_conditional(id_cmp, id_exit, id_body)

    # body
    b.instr_label(id_body)

    # word = src.data[gid]
    id_src_elem_ptr = b.new_id()
    b.instr_access_chain(id_ptr_ssbo_uint, id_src_elem_ptr, id_src, id_uint_0, id_gid)
    id_word = b.new_id()
    b.instr_load(id_uint, id_word, id_src_elem_ptr)

    # unpack bytes
    def unpack_byte(builder, word_id, shift_id, mask_id):
        if shift_id is None:
            shifted = word_id
        else:
            shifted = builder.new_id()
            builder.instr_shift_right_logical(id_uint, shifted, word_id, shift_id)
        byte_id = builder.new_id()
        builder.instr_bitwise_and(id_uint, byte_id, shifted, mask_id)
        return byte_id

    id_b0 = unpack_byte(b, id_word, None, id_uint_255)
    id_b1 = unpack_byte(b, id_word, id_uint_8, id_uint_255)
    id_b2 = unpack_byte(b, id_word, id_uint_16, id_uint_255)
    id_b3 = unpack_byte(b, id_word, id_uint_24, id_uint_255)

    # bias = params.bias (member 0)
    id_bias_ptr = b.new_id()
    b.instr_access_chain(id_ptr_push_float, id_bias_ptr, id_params, id_uint_0)
    id_bias = b.new_id()
    b.instr_load(id_float, id_bias, id_bias_ptr)

    def process_channel(builder, byte_id):
        f_id = builder.new_id()
        builder.instr_convert_u_to_f(id_float, f_id, byte_id)
        fadd_id = builder.new_id()
        builder.instr_fadd(id_float, fadd_id, f_id, id_bias)
        clamped_id = builder.new_id()
        # GLSL.std.450 FClamp = 43
        builder.instr_ext_inst(id_float, clamped_id, builder.ext_glsl_id, 43, fadd_id, id_float_0, id_float_255)
        result_id = builder.new_id()
        builder.instr_convert_f_to_u(id_uint, result_id, clamped_id)
        return result_id

    id_r0 = process_channel(b, id_b0)
    id_r1 = process_channel(b, id_b1)
    id_r2 = process_channel(b, id_b2)

    # pack
    id_r1_shift = b.new_id()
    b.instr_shift_left_logical(id_uint, id_r1_shift, id_r1, id_uint_8)
    id_r2_shift = b.new_id()
    b.instr_shift_left_logical(id_uint, id_r2_shift, id_r2, id_uint_16)
    id_b3_shift = b.new_id()
    b.instr_shift_left_logical(id_uint, id_b3_shift, id_b3, id_uint_24)

    id_packed_01 = b.new_id()
    b.instr_bitwise_or(id_uint, id_packed_01, id_r0, id_r1_shift)
    id_packed_012 = b.new_id()
    b.instr_bitwise_or(id_uint, id_packed_012, id_packed_01, id_r2_shift)
    id_packed = b.new_id()
    b.instr_bitwise_or(id_uint, id_packed, id_packed_012, id_b3_shift)

    # dst.data[gid] = packed
    id_dst_elem_ptr = b.new_id()
    b.instr_access_chain(id_ptr_ssbo_uint, id_dst_elem_ptr, id_dst, id_uint_0, id_gid)
    b.instr_store(id_dst_elem_ptr, id_packed)

    b.instr_branch(id_exit)

    # exit
    b.instr_label(id_exit)
    b.instr_return()
    b.instr_function_end()

    return b.build()


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("brightness.spv")
    spv = build_brightness_shader()
    out_path.write_bytes(spv)
    print(f"Wrote {len(spv)} bytes SPIR-V to {out_path}")


if __name__ == "__main__":
    main()
