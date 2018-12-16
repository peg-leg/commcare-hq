/* global ko */
/* global django */
/* global zxcvbn */

ko.validation.rules['phone_number_val'] = {


  // var: input = document.querySelector("#id_phone_number"),
  //     var: errorMsg = document.querySelector("#id_phone_number"),
  //     var: validMsg = document.querySelector("#id_phone_number"),

    // on blur: validate
        validator: function (val) {
        return val === 5;
          // if (input.value.trim()) {
          //   if ($(this).intlTelInput("isValidNumber")) {
          //     validMsg.classList.remove("hide");
          //   } else {
          //     input.classList.add("error");
          //     var errorCode = $(this).intlTelInput("getValidationError");
          //     errorMsg.innerHTML = errorMap[errorCode];
          //     errorMsg.classList.remove("hide");
          //   }
          // }
    },



    message: django.gettext("PREETHI - DEFAULT PHONE VALIDATION MESSAGE"),







};

ko.validation.registerExtenders();
